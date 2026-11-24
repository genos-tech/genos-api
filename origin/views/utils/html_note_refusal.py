"""What a REST write that would change an HTML note's placeholder text answers (MIP-2218).

An HTML note's `body` is only a readable placeholder made from its page (the page is in
`NoteHtmlRevision`). A write into the placeholder would change what search and `get_note` show
without changing the page, so every such path refuses with a sentence that says what to do instead
and writes nothing, as the MCP tools `update_note` and `edit_note_block` already do.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

from origin.services import html_notes

SUGGESTION = (
    "This is an HTML note: its text is made from its page, so the text cannot be saved here and "
    "nothing was changed. To change the page, replace it (`save_html_note` with this note's "
    "`note_id` and the whole new page; pass `title` there to rename it). Moving the note to "
    "another folder and sharing it work as for any note."
)


def is_html(note) -> bool:
    return getattr(note, "body_format", None) == html_notes.BODY_FORMAT_HTML


def html_note_put_refusal(note) -> Response | None:
    """A 409 answer that guides the caller when `note` is an HTML note; `None` otherwise."""
    if not is_html(note):
        return None
    return Response(
        {"detail": SUGGESTION, "body_format": html_notes.BODY_FORMAT_HTML},
        status=status.HTTP_409_CONFLICT,
    )
