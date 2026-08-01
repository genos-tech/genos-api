"""Authorization probe for the Hocuspocus collab server.

`genos-collab` holds Yjs documents keyed by an opaque `documentName` and,
before this endpoint, decided for itself what each name meant: it parsed
the `my-note:` / `task-note:` / `chat-note:` prefixes in JavaScript,
mapped them to Django's numeric note-type constants by hand, and let
**every other prefix through unauthenticated** — including `task-body:`,
which is a real document the client opens for every task. Any valid JWT
could therefore read and write the body of any task in any project.

Two problems, one cause: the document-name grammar lived in a second
repo, and anything the grammar didn't recognise defaulted to allow.

This endpoint moves both halves to the side that owns the ACL. Collab
now asks one question — *"may this user open this document name?"* — and
gets 200 or 403. Adding a new collaborative surface means teaching Django
about a prefix, not editing two repos and hoping they agree. Unknown
prefixes are **denied**, so the default flipped from allow to deny.

Deploy order matters: this endpoint must be live before the collab change
that calls it, or every document load fails closed.
"""

import logging

from rest_framework import status
from rest_framework.response import Response

from origin.models.task.task_models import TaskMaster
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.note_role import (
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    get_effective_role,
    note_exists,
)
from origin.views.utils.scope_guards import is_project_member

logger = logging.getLogger(__name__)

# The document-name grammar, in the one place that can enforce it.
# Mirrors the prefixes genos-collab's editors use; see
# `frontend/src/components/editors/bn*Editor.tsx`.
_NOTE_PREFIXES = {
    "my-note": NOTE_TYPE_PERSONAL,
    "task-note": NOTE_TYPE_TASK,
    "chat-note": NOTE_TYPE_CHAT,
}
_TASK_BODY_PREFIX = "task-body"


def parse_document_name(document_name):
    """`"task-note:12"` -> `("task-note", 12)`; `None` if malformed.

    Deliberately strict: the id must be a positive integer, because every
    surface behind this grammar is keyed by one. A name we cannot parse
    is refused rather than passed through.
    """
    if not document_name or not isinstance(document_name, str):
        return None
    prefix, sep, raw_id = document_name.partition(":")
    if not sep:
        return None
    try:
        entity_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    if entity_id <= 0:
        return None
    return prefix, entity_id


class CollabAccessCheckView(AuthenticatedAPIView):
    """`POST {"document_name": "task-note:12"}` -> 200 / 403 / 404.

    JWT auth already establishes who is asking; a `user_id` in the body
    would be a claim, so none is accepted (same rule as
    `NoteRoleCheckView`).
    """

    def post(self, request):
        document_name = request.data.get("document_name")
        parsed = parse_document_name(document_name)
        if parsed is None:
            return Response(
                {"error": "document_name must look like '<prefix>:<id>'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        prefix, entity_id = parsed
        user_id = request.user.id

        if prefix in _NOTE_PREFIXES:
            return self._check_note(user_id, _NOTE_PREFIXES[prefix], entity_id)
        if prefix == _TASK_BODY_PREFIX:
            return self._check_task_body(user_id, entity_id)

        # Default deny. This is the branch that used to be "allow".
        logger.warning("collab access check: unknown document prefix %r", prefix)
        return Response(
            {"error": "Unknown document type."},
            status=status.HTTP_403_FORBIDDEN,
        )

    @staticmethod
    def _check_note(user_id, note_type, note_id):
        if not note_exists(note_type, note_id):
            return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        role = get_effective_role(user_id, note_type, note_id)
        if role is None:
            return Response(
                {"error": "You do not have access to this document."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response({"role_id": role}, status=status.HTTP_200_OK)

    @staticmethod
    def _check_task_body(user_id, task_id):
        """A task body is readable by the same people as the task itself:
        project members, plus the assignee and reporter — mirroring
        `search_engine/agent/acl.task_acl_user_ids`, which is what decides
        whether the task shows up in search."""
        task = (
            TaskMaster.objects.filter(task_id=task_id, is_deleted=False)
            .values("project_id", "assignee_id", "reporter_id")
            .first()
        )
        if task is None:
            return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        allowed = is_project_member(task["project_id"], user_id) or str(user_id) in {
            str(task["assignee_id"]),
            str(task["reporter_id"]),
        }
        if not allowed:
            return Response(
                {"error": "You do not have access to this document."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response({"role_id": None}, status=status.HTTP_200_OK)
