"""`GET /api/v3/notes/<note_type>/<note_id>/html`: the saved page of an HTML note (MIP-2214).

The note's own ACL decides who may read it: `require_read_role` over `get_effective_role`, the rule
every other note read uses, so a person who can open the note can read its page and nobody else can
(an unknown, other-team and unreadable note all answer the same 404). `note_type` is the family's
name (`personal`, which also holds Team Notes, `task`, `chat`) or its number (1, 2, 3).

The answer is JSON, never `text/html`: the page is untrusted text the viewer puts in a sandboxed
frame, and a browser must not be able to render it from this URL. `no-store` keeps a shared cache
from holding a private page, `nosniff` stops a type guess. Off (`HTML_NOTES` false) every route
answers 404 as if it did not exist.
"""

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response

from origin.services import html_notes
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.note_role import (
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    require_read_role,
)

_TYPES = {
    "personal": NOTE_TYPE_PERSONAL,
    "task": NOTE_TYPE_TASK,
    "chat": NOTE_TYPE_CHAT,
    "1": NOTE_TYPE_PERSONAL,
    "2": NOTE_TYPE_TASK,
    "3": NOTE_TYPE_CHAT,
}
_FAMILY = {NOTE_TYPE_PERSONAL: "personal", NOTE_TYPE_TASK: "task", NOTE_TYPE_CHAT: "chat"}


def _not_found():
    return Response({"error": "Note not found."}, status=status.HTTP_404_NOT_FOUND)


class HtmlNoteView(AuthenticatedAPIView):
    def get(self, request, note_type, note_id):
        if not getattr(settings, "HTML_NOTES", False):
            return _not_found()
        code = _TYPES.get(str(note_type).lower())
        if code is None:
            return _not_found()

        if denied := require_read_role(request.user.id, code, note_id):
            return denied

        if html_notes.body_format_of(code, note_id) != html_notes.BODY_FORMAT_HTML:
            return Response(
                {"error": "This note is not an HTML note."}, status=status.HTTP_404_NOT_FOUND
            )
        revision = html_notes.latest_revision(code, note_id)
        if revision is None:
            return _not_found()

        response = Response(
            {
                "note_type": _FAMILY[code],
                "note_id": note_id,
                "rev": revision.rev,
                "html": revision.html,
                "sha256": revision.sha256,
                "size": revision.size,
                "warnings": revision.warnings,
                "saved_at": revision.ts_created_at.isoformat(),
            },
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
