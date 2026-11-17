"""`comment_on_note` for the in-app agent, behind `AGENT_INAPP_NOTE_COMMENT_TOOL` (MIP-1671).

The MCP tool of the same name (MIP-1644, `views/public/mcp/note_threads/comment_on_note.py`) starts a comment
thread on a passage of a note. This is its in-app twin, the way `get_timeline` has one (`get_timeline.py`): the
same work, run by the in-app model, behind a flag that is OFF by default.

**Dark by default, on purpose.** A new tool is model-visible: it adds its declaration to every request and can
take an intent from a tool the model already reaches for (a comment on a TASK is `add_comment`; a rewrite of a
note is `update_note`). genos-docs `spotlight/AGENT_TOOL_SURFACE.md` §6 says what such a change needs: a routing and
behaviour eval, and the prompt budget raised in the same PR on that evidence. None of that has been run, so with
the flag off the tool is neither declared nor runnable (`tools/base.py::FLAGGED_REGISTRY`) and the prompt and
declarations are byte-identical to before. The eval, its arm and the budget are the step that flips it.

**One implementation.** The work (finding the passage, the thread and its mark in the live document, making the
document of a note nobody opened, telling the people the comment reaches) is the MCP tool's `_run`, imported when
the tool runs: the service layer imports `tools.base`, which runs this package's `__init__`, which imports this
module, so a top-level import would be circular. Only the wording the model reads differs, and one argument: the
in-app model has no block ids (`fetch_note` does not list them), so there is no `block_id`.

**Approval.** A write, so the member sees a card and answers it before anything is posted under their name. Its
text is not editable on the card and it cannot be undone (`approval_edits.EDITABLE_FIELDS`, `undo.UNDO_SUPPORT`):
the people it reaches are told, and removing a thread is not built.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools.base import Tool, ToolContext

MAX_TEXT_CHARS = 4000
MAX_ANCHOR_CHARS = 500


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # Imported here: see the module docstring.
    from origin.views.public.mcp.note_threads.comment_on_note import _run as run  # noqa: PLC0415

    # The model has no block ids to pass; anything it invents is ignored, not trusted.
    return run({k: v for k, v in args.items() if k != "block_id"}, ctx)


COMMENT_ON_NOTE_INAPP = Tool(
    name="comment_on_note",
    description=(
        "Start a comment thread on a PASSAGE of a note, as the member (a comment on a TASK is "
        "add_comment; changing the note's own text is update_note). Read the note with fetch_note "
        "first and copy the passage EXACTLY from it. REQUIRES USER APPROVAL. `anchor_text` is the "
        "words the comment is about: same case and spacing, no markdown marks, inside one "
        "paragraph, heading, list item, quote or table cell, not across an @mention. If it is not "
        "found, or is found more than once, nothing is posted and the answer says why: pass "
        "`occurrence` (1 = the first) when it repeats. Code, math and diagram blocks cannot hold a "
        "comment. `text` is the comment, plain text, no formatting or @-mentions. The note's "
        "readers are told. Returns `thread_id`."
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
            "anchor_text": {
                "type": "STRING",
                "description": (
                    "The passage, copied exactly from the note "
                    f"(at most {MAX_ANCHOR_CHARS} characters)."
                ),
            },
            "text": {
                "type": "STRING",
                "description": f"The comment, plain text (at most {MAX_TEXT_CHARS} characters).",
            },
            "occurrence": {
                "type": "INTEGER",
                "description": "Which occurrence when the passage repeats (1 = the first).",
            },
        },
        "required": ["note_type", "note_id", "anchor_text", "text"],
    },
    run=_run,
    requires_approval=True,
)
