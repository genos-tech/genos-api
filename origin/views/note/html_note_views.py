"""`GET /api/v3/notes/<note_type>/<note_id>/html`: the saved page of an HTML note (MIP-2214).

The note's own ACL decides who may read it: `require_read_role` over `get_effective_role`, the rule
every other note read uses, so a person who can open the note can read its page and nobody else can
(an unknown, other-team and unreadable note all answer the same 404). `note_type` is the family's
name (`personal`, which also holds Team Notes, `task`, `chat`) or its number (1, 2, 3).

The answer is JSON, never `text/html`: the page is untrusted text, and a browser must not be able to
render it from this URL (it is on the cookie host and carries no policy). `no-store` keeps a shared
cache from holding a private page, `nosniff` stops a type guess. Off (`HTML_NOTES` false) every route
answers 404 as if it did not exist.

To SHOW the page the viewer asks `.../html/frame` (`HtmlNoteFrameView`) for a short-lived URL and puts
that in a sandboxed `<iframe src>`; `GET /h/<token>` (`HtmlNotePageView`) answers it as `text/html`
under its own header policy (MIP-2228, `services/html_note_pages.py`).
"""

from typing import Any

from django.conf import settings
from rest_framework import permissions, status
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.renderers import StaticHTMLRenderer
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from origin.search_engine.agent.model_fence import escape_user_arguments
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.services import html_note_pages, html_notes
from origin.services.throttles import RecordedThrottleMixin, WindowRateThrottle
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


class HtmlNoteFrameView(AuthenticatedAPIView):
    """`GET /api/v3/notes/<note_type>/<note_id>/html/frame` (MIP-2228): the URL the viewer's frame loads.

    The note's own read ACL decides (`require_read_role`, the same 404 for an unknown, other-team and
    unreadable note), then this signs a short-lived token for the note's latest revision and answers
    `{path, nonce, rev, expires_in}`. `path` is relative to the API host (`/h/<token>`, served by
    `HtmlNotePageView`, ONCE: every frame load asks for a fresh one); `nonce` is the secret the page's
    first script reports back, given to nobody but this caller (the page route spends the URL before
    any script of the page runs, so the page cannot hand the URL on to learn it). Off (`HTML_NOTES`
    false) every call is the 404 of a route that is not there.
    """

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

        token = html_note_pages.mint(code, note_id, revision.rev)
        response = Response(
            {
                "path": f"{html_note_pages.PAGE_PATH_PREFIX}{token}",
                "nonce": html_note_pages.nonce_for(token),
                "rev": revision.rev,
                "size": revision.size,
                "warnings": revision.warnings,
                "saved_at": revision.ts_created_at.isoformat(),
                "expires_in": html_note_pages.TOKEN_SECONDS,
            },
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class HtmlNotePageThrottle(RecordedThrottleMixin, AnonRateThrottle):
    """Anyone can call the route, so this caps what a caller can make it do (a signature check and, for
    a good token, two queries): one frame load is one request, a few reloads a minute at most."""

    rate = "120/min"
    scope = "html_note_page"


class _AcceptAnything(BaseContentNegotiation):
    """A browser navigation sends `Accept: text/html,...`; a tool may send anything. The route has one
    answer, so the header never turns it into a 406."""

    def select_parser(self, request, parsers):
        return parsers[0] if parsers else None

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


class HtmlNotePageView(APIView):
    """`GET /h/<token>` (MIP-2228): the saved page itself, as its own sandboxed document.

    PUBLIC on purpose: the frame that loads it has no credentials (and must not have), and the URL is
    the capability, a token signed by `HtmlNoteFrameView` after the note's own ACL, bound to one
    revision, good once and for `html_note_pages.TOKEN_SECONDS` at most. A bad, altered, expired, spent
    or unknown token, a page that is gone and a request that is not a frame load all answer the same
    short 404, and so does the route while `HTML_NOTES` is off.

    Only a FRAME load is served: a browser that says `Sec-Fetch-Dest` other than `iframe` (a tab opened
    on the URL, an `<img>`, a `fetch`) is refused WITHOUT spending the token. The sandbox keeps a page
    harmless even then, but a page opened as a tab on this host is a page nobody watches for
    self-navigation, so it is simply not offered. (A client that sends no such header is no browser
    that could be walked to the URL; the token is still single-use and short.)

    No authentication class runs (an `Authorization` header is never parsed) and no cookie is read or
    set. The header policy is `html_note_pages.content_security_policy`: see that module for why a
    script in the page can run and still reach nothing.
    """

    # No HEAD or OPTIONS: a HEAD would be answered as a GET and spend the token for nothing.
    http_method_names = ["get"]
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [HtmlNotePageThrottle]
    renderer_classes = [StaticHTMLRenderer]
    content_negotiation_class = _AcceptAnything

    def get(self, request, token):
        gone = self._harden(
            Response("This page is not available.", status=status.HTTP_404_NOT_FOUND)
        )
        if not getattr(settings, "HTML_NOTES", False):
            return gone
        if request.headers.get("Sec-Fetch-Dest", "iframe").lower() != "iframe":
            return gone
        grant = html_note_pages.read(str(token))
        if grant is None:
            return gone
        # The token says which revision; the note must still be an HTML note with that revision (a
        # deleted note takes its revisions with it, an overwritten one only its oldest beyond the cap).
        revision = html_notes.revision_of(grant.note_type, grant.note_id, grant.rev)
        if revision is None:
            return gone
        if html_notes.body_format_of(grant.note_type, grant.note_id) != html_notes.BODY_FORMAT_HTML:
            return gone
        # Last, so a request refused above leaves the token for the frame it was minted for.
        if not html_note_pages.consume(str(token)):
            return gone

        response = self._harden(
            Response(
                html_note_pages.document(revision.html, html_note_pages.nonce_for(str(token))),
                status=status.HTTP_200_OK,
            )
        )
        response["Content-Security-Policy"] = html_note_pages.content_security_policy()
        # Django's clickjacking middleware would add `X-Frame-Options: DENY`, which beats
        # `frame-ancestors` and would stop the app from framing its own page; the policy says who may.
        response.xframe_options_exempt = True
        return response

    @staticmethod
    def _harden(response):
        response["Cache-Control"] = "private, no-store"
        response["Referrer-Policy"] = "no-referrer"
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
            # `run` is wrapped by `with_unfenced_arguments`, which un-escapes a fence tag the in-app
            # MODEL copied back. An uploaded file is a person's text, nobody's copy of a fence:
            # escape it first so the wrapper hands the tool the exact bytes (MIP-2301, as MIP-2300).
            out = SAVE_HTML_NOTE.run(escape_user_arguments(args), ctx)
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
