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

from typing import Any

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response

from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.services import html_notes
from origin.services.throttles import WindowRateThrottle
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.note_role import (
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    require_read_role,
)
from origin.views.utils.request_validators import as_uuid
from origin.views.utils.scope_guards import require_team_member_or_response

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


class HtmlNoteImportThrottle(WindowRateThrottle):
    # The plan's own saves-per-hour limit is the real brake (`html_notes.check_rate`); this only keeps
    # a loop of requests from making the server read pages it will refuse.
    scope = "html_note_import"
    rate = "60/min"
    fail_closed = True


#: What an import may name; `note_id` is not here on purpose: an import never replaces a page.
_IMPORT_ARGS = ("title", "note_type", "folder_id", "project_id", "task_id", "chat_id", "thread_id")


class HtmlNoteImportView(AuthenticatedAPIView):
    """`POST /api/v3/notes/html/import/` (MIP-2215): import a `.html` file as a new HTML note.

    The web client's twin of the MCP tool `save_html_note`: it CALLS that tool's own function, so the
    plan gate (Free: nothing is stored and the answer is a suggestion, never an error status), the
    page size, the count of HTML notes, the monthly note quota, the saves per hour, the scan, the
    target rules (`folder_id` for a team note, `project_id` and `task_id` for a task note, `chat_id`
    for a chat note) and the access checks are the MCP's, with no second copy to drift.

    Request (JSON): `team_id`, `html`, `note_type` ("personal" | "team" | "task" | "chat") and the
    target fields above; `title` defaults to the page's `<title>`. Answers: 201 the note, 200
    `{saved: false, plan_required, your_plan}` for a plan without HTML notes, 400 a refusal that says
    what to do (`error`), 403 no right to write there, 404 a team the caller is not in. Off
    (`HTML_NOTES` false) every call is the 404 of a route that does not exist.
    """

    throttle_classes = [HtmlNoteImportThrottle]

    def post(self, request):
        if not getattr(settings, "HTML_NOTES", False):
            return _not_found()
        data = request.data if isinstance(request.data, dict) else {}
        team = as_uuid(data.get("team_id"))
        if team is None:
            return Response({"error": "`team_id` is required."}, status=status.HTTP_400_BAD_REQUEST)
        if (refused := require_team_member_or_response(request.user, str(team))) is not None:
            return refused

        from origin.views.public.mcp.html_notes.save_html_note import SAVE_HTML_NOTE  # noqa: PLC0415

        args: dict[str, Any] = {"html": data.get("html")}
        args.update({key: data[key] for key in _IMPORT_ARGS if data.get(key) not in (None, "")})
        ctx = ToolContext(team_id=str(team), user_id=str(request.user.id))
        try:
            out = SAVE_HTML_NOTE.run(args, ctx)
        except ToolForbidden as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ToolError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if not out.get("note_id"):
            # A plan without HTML notes: `HtmlNotesNotOnPlan.reply()`, nothing was stored.
            return Response(
                {
                    "saved": False,
                    "plan_required": out.get("plan_required"),
                    "your_plan": out.get("your_plan"),
                },
                status=status.HTTP_200_OK,
            )
        body = {
            "saved": True,
            "note_id": out["note_id"],
            "note_type": out["note_type"],
            "title": out["title"],
            "html_rev": out["html_rev"],
            "size_bytes": out["size_bytes"],
            # `no_summary` is advice to a model, which can add a summary; a person importing a file
            # has none to give.
            "warnings": [w for w in out.get("warnings", []) if w.get("code") != "no_summary"],
        }
        for key in ("space", "folder_id", "project_id", "task_id", "path"):
            if out.get(key) is not None:
                body[key] = out[key]
        return Response(body, status=status.HTTP_201_CREATED)
