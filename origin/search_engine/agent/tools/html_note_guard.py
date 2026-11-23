"""The refusal an HTML note gives to the tools that edit a note's TEXT (MIP-2214).

An HTML note's `body` is only the readable placeholder made from its page; the page is in
`NoteHtmlRevision`. Writing into the placeholder (`update_note`, `edit_note_block`) would change
what search finds without changing the page, so those tools answer with the way to do it right and
write nothing. Kept here (a plain module with no tool in it) so both tools share one sentence.
"""

from __future__ import annotations

from origin.search_engine.agent.tools.base import ToolError

BODY_FORMAT_HTML = "html"


def is_html_note(note) -> bool:
    return getattr(note, "body_format", None) == BODY_FORMAT_HTML


def refuse_html_text_edit(note, note_id: int) -> None:
    """Raise a `ToolError` that says what to do, when `note` is an HTML note; else return."""
    if not is_html_note(note):
        return
    raise ToolError(
        f"Note {note_id} is an HTML note: its text is made from its page, so it cannot be edited "
        "here. Nothing was changed. To change the page, call `save_html_note` with "
        f"`note_id` {note_id} and the WHOLE new page as `html` (read the current page first with "
        "`get_note` and `include_html: true`); pass `title` there to rename it. Moving the note "
        "to another folder and sharing it work as for any note."
    )
