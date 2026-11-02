"""Which inbox item may authorize an approval (MIP-940).

Approving a join request adds a person to a team or a project, so the item that
asks for it is the whole authorization and cannot be taken on trust. It used to
be: the lookup was `receiver=request.user` and nothing else, and `POST /inbox/`
let the caller choose both the receiver and the type, so a "request" addressed
to yourself was enough to add yourself to any project in your team, or to
another team given its id.

An item is a request only if all of these hold, and every failure is the same
404 so nothing says which item ids exist:

* it is addressed to the caller, is not deleted, and is of the type this
  endpoint answers;
* someone else sent it: a request you sent yourself proves nothing;
* the team in the request body, if there is one, is the item's team.

The caller must also be allowed to add people to the thing named in it, which
only the view can check.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

from origin.models.common.inbox_models import InboxItems

REQUEST_PENDING = "pending"
REQUEST_APPROVED = "approved"

# `item_type`s of the requests an approval answers (see `InboxItems`).
JOIN_TEAM_REQUEST = 1
JOIN_PROJECT_REQUEST = 2


def not_found() -> Response:
    return Response({"error": "Inbox item not found."}, status=status.HTTP_404_NOT_FOUND)


def already_answered() -> Response:
    return Response(
        {"error": "This request was already answered."}, status=status.HTTP_409_CONFLICT
    )


def load_request_item(request, *, item_type: int) -> tuple[InboxItems | None, Response | None]:
    """The request item `request.data["item_id"]` names, or the response to send.

    A rejected request is refused here. An approved one is let through so that a
    repeated click, or a retry after the approval half-finished, stays harmless:
    the caller adds nobody for it unless it is still `pending`
    (`is_still_open`).
    """
    try:
        item_id = int(request.data.get("item_id"))
    except (TypeError, ValueError):
        return None, Response({"error": "item_id is required."}, status=status.HTTP_400_BAD_REQUEST)

    item = InboxItems.objects.filter(
        item_id=item_id, receiver=request.user, item_type=item_type, is_deleted=False
    ).first()
    if (
        item is None
        or item.sender_id is None
        or item.team_id is None
        or str(item.sender_id) == str(request.user.id)
    ):
        return None, not_found()

    claimed_team = request.data.get("team_id")
    if claimed_team is not None and str(claimed_team) != str(item.team_id):
        return None, not_found()

    if item.request_status not in (REQUEST_PENDING, REQUEST_APPROVED):
        return None, already_answered()
    return item, None


def is_still_open(item: InboxItems) -> bool:
    """May this item still add someone? Only while nobody has answered it."""
    return item.request_status == REQUEST_PENDING
