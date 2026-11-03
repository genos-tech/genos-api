"""Which inbox item may authorize an approval (MIP-940).

Approving a join request adds a person to a team or a project, so the item that
asks for it is the whole authorization and cannot be taken on trust. It used to
be: the lookup was `receiver=request.user` and nothing else, and `POST /inbox/`
let the caller choose both the receiver and the type, so a "request" addressed
to yourself was enough to add yourself to any project in your team, or to
another team given its id.

An item is a request only if it is addressed to the caller, is not deleted, and
is of the type the endpoint answers. Every failure is the same 404, so nothing
says which item ids exist.

What the item may be used for is the view's to check, because only the view
knows the thing it names:

* the team and the project are the ITEM's, never what the request body says;
* the caller must be allowed to add people to that team or project; and
* the item must still be pending (`is_still_open`), so an old approval cannot
  put someone back who has since left.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

from origin.models.common.inbox_models import InboxItems

REQUEST_PENDING = "pending"

# `item_type`s of the requests an approval answers (see `InboxItems`).
JOIN_TEAM_REQUEST = 1
JOIN_PROJECT_REQUEST = 2


def not_found() -> Response:
    return Response({"error": "Inbox item not found."}, status=status.HTTP_404_NOT_FOUND)


def already_answered() -> Response:
    return Response(
        {"error": "This request was already answered."}, status=status.HTTP_409_CONFLICT
    )


def find_request_item(user, item_id: int, *, item_type: int) -> InboxItems | None:
    """The request of `item_type` addressed to `user` that `item_id` names, or None.

    The one place that says what counts as a request, for the pages (through
    `load_request_item`) and for the agent's tools (MIP-882)."""
    item = InboxItems.objects.filter(
        item_id=item_id, receiver=user, item_type=item_type, is_deleted=False
    ).first()
    if item is None or item.sender_id is None or item.team_id is None:
        return None
    return item


def load_request_item(request, *, item_type: int) -> tuple[InboxItems | None, Response | None]:
    """The request item `request.data["item_id"]` names, or the response to send."""
    try:
        item_id = int(request.data.get("item_id"))
    except (TypeError, ValueError):
        return None, Response({"error": "item_id is required."}, status=status.HTTP_400_BAD_REQUEST)

    item = find_request_item(request.user, item_id, item_type=item_type)
    if item is None:
        return None, not_found()
    return item, None


def is_still_open(item: InboxItems) -> bool:
    """May this item still add someone? Only while nobody has answered it."""
    return item.request_status == REQUEST_PENDING
