"""`save_html_note` (MIP-2214): MCP-only, see `html_notes/__init__.py`. The create/replace core is `services/html_notes.save_page`.

Stores an HTML page as a note, or replaces the page of an HTML note, and answers with what the page
needs to be fixed. The save NEVER fails for a fixable reason: it stores and warns. HTML notes are a
Core feature with per-plan limits (`services/html_note_limits.py`, the one table): a Free member's
call stores nothing and is answered with a suggestion (not an error); the page size, the storage
quota and the hourly rate are the plan's numbers (the number of HTML notes is not limited). The only things
refused are those limits (worded as what to do), a note that is not an HTML
note (a BlockNote note is never converted: the answer suggests a new note), and what any note write
refuses (no access, archived, wrong target).

A new note is made by `create_note`'s own helpers, so the target, the ACL, the owner row, the
first version and the monthly note quota are exactly what `create_note` gives; this module adds
the page. A replace is checked like `update_note`: owner or editor of the note.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.services import html_note_limits, html_notes
from origin.views.public.mcp.html_notes.guide import rules_text

log = logging.getLogger(__name__)


def _feature_on() -> None:
    # Defence in depth: the catalog does not list the tool while the switch is off and `call`
    # answers as for an unpublished name, so this only matters to a direct caller.
    if not getattr(settings, "HTML_NOTES", False):
        raise ToolError("HTML notes are not available yet.")


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """The MCP entry point: the switch, then the shared core (`html_notes.save_page`, MIP-2352)."""
    _feature_on()
    return html_notes.save_page(args, ctx)


def _size_limits_text() -> str:
    core = html_note_limits.HTML_NOTE_LIMITS["core"]
    pro = html_note_limits.HTML_NOTE_LIMITS["pro"]
    return f"{core.max_bytes // 1024} KB on Core, {pro.max_bytes // 1024} KB on Pro and up"


def _storage_limits_text() -> str:
    core = html_note_limits.HTML_NOTE_LIMITS["core"]
    pro = html_note_limits.HTML_NOTE_LIMITS["pro"]
    size = html_note_limits.size_text
    return f"{size(core.max_storage_bytes)} on Core, {size(pro.max_storage_bytes)} on Pro and up"


def _rate_limits_text() -> str:
    core = html_note_limits.HTML_NOTE_LIMITS["core"]
    pro = html_note_limits.HTML_NOTE_LIMITS["pro"]
    return f"{core.saves_per_hour} on Core, {pro.saves_per_hour} on Pro and up"


SAVE_HTML_NOTE = Tool(
    name="save_html_note",
    description=(
        "Save a self-contained HTML page as a read-only note (a dashboard, report, diagram or "
        "explainer). REQUIRES USER APPROVAL. Pass `html` (the whole page) and `note_type` "
        "with `title`, as for `create_note`."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "html": {
                "type": "STRING",
                "description": (
                    "The whole page, starting with <!doctype html>. "
                    f"{_size_limits_text()} per page, by plan."
                ),
            },
            "title": {
                "type": "STRING",
                "description": (
                    "The note's title. Defaults to the page's <title>; required if the page has "
                    "none. On a replace it renames the note; omit it to keep the title."
                ),
            },
            "summary": {
                "type": "STRING",
                "description": (
                    "1 to 3 sentences saying what the page shows. It is what search finds, so "
                    "always pass it."
                ),
            },
            "note_id": {
                "type": "INTEGER",
                "description": (
                    "Only to REPLACE the page of an existing HTML note (with `note_type`). "
                    "Omit it to create a new note."
                ),
            },
            "note_type": {
                "type": "STRING",
                "enum": ["personal", "team", "task", "chat"],
                "description": (
                    "Which kind of note: 'personal' (private), 'team' (a Team Notes folder), "
                    "'task' (a project) or 'chat'. Required to create, and with `note_id`."
                ),
            },
            "folder_id": {
                "type": "INTEGER",
                "description": (
                    "A personal note: one of your My-Notes folders (optional). A team note: the "
                    "Team Notes folder (required)."
                ),
            },
            "project_id": {
                "type": "INTEGER",
                "description": "Required when note_type='task': the project the note belongs to.",
            },
            "task_id": {
                "type": "INTEGER",
                "description": "Optional, with note_type='task': attach the note to one task.",
            },
            "chat_id": {
                "type": "STRING",
                "description": "Required when note_type='chat': the chat's id or Genos URL.",
            },
            "thread_id": {
                "type": "STRING",
                "description": "Optional, with note_type='chat': attach to one thread.",
            },
        },
        "required": ["html"],
    },
    run=_run,
    requires_approval=True,
    mcp_only=True,
)


def mcp_description() -> str:
    """The published description: what the tool does, then the rules for writing the page."""
    return (
        "Save an HTML page as a read-only note in Genos: a dashboard, a report, a diagram, an "
        "explainer. Use it when the user asks you to show, draw or describe something visually "
        "and keep it. It creates a note of any kind `create_note` does ('personal', 'team' with "
        "`folder_id`, 'task' with `project_id`, 'chat' with `chat_id`) under the same sharing "
        "rules as every note, or, with `note_id` and `note_type`, REPLACES the page of an HTML "
        "note you can edit (the earlier page is kept as a revision). HTML notes are a Core "
        "plan feature: on the Free plan nothing is stored and the answer says what to tell the "
        "user. The number of HTML notes is not limited; each note owner has a storage quota "
        f"that counts every kept version, up to {html_notes.KEEP_REVISIONS} per note "
        f"({_storage_limits_text()}). It stores the page and warns: the save is refused only "
        f"when the page is over the plan's size ({_size_limits_text()}), it would take the "
        "owner's HTML storage past the quota, or the plan's saves per hour are used "
        f"({_rate_limits_text()}); each refusal says what to do. Anything else the page needs "
        "is returned as `warnings`, each with a fix in `suggestions`: read them and save again "
        "with `note_id` if you can improve the "
        "page. The page is shown in a sandboxed frame with no network, so follow these rules. "
        + rules_text()
        + " An ordinary note is never converted into an HTML note: to make a page, create a "
        "new note. `update_note` and `edit_note_block` do not change an HTML note's page: use "
        "this tool with `note_id`. Read a page back with `get_note` and `include_html: true`. "
        "Requires a write-scoped API key."
    )
