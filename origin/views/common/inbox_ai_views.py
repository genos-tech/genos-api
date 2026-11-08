"""Turn off what spent a member's AI usage, from its Inbox item (MIP-1209).

    GET  /api/v2/inbox/<item_id>/ai/        the item's AI source and whether it is on
    POST /api/v2/inbox/<item_id>/ai/off/    turn it off (the digest, or that schedule)
    POST /api/v2/inbox/<item_id>/ai/on/     the undo

An Inbox item that used AI carries its cost in `itemOptionals.ai` and the list gives each such
item an `aiSource` (see `services/inbox_ai.py`). These three act on the SOURCE of the item, read
on the server from the item itself (never from the request), and only on the caller's own items:
an item that is someone else's, deleted, or not an AI item is the same 404. Nothing is deleted:
past items stay. Every answer is JSON with a plain sentence in `what` (or `error`).
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from origin.models.common.inbox_models import InboxItems
from origin.services import inbox_ai, schedules
from origin.services.throttles import WindowRateThrottle
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


class InboxAiThrottle(WindowRateThrottle):
    scope = "inbox_ai"
    rate = "60/min"
    fail_closed = True


def _not_found() -> Response:
    return Response({"error": "Inbox item not found."}, status=status.HTTP_404_NOT_FOUND)


class _Base(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]
    throttle_classes = [InboxAiThrottle]

    def _item(self, request, item_id: int):
        return InboxItems.objects.filter(
            item_id=item_id, receiver=request.user, is_deleted=False
        ).first()


class InboxAiSourceView(_Base):
    def get(self, request, item_id: int):
        item = self._item(request, item_id)
        source = inbox_ai.source_of(request.user, item) if item else None
        if source is None:
            return _not_found()
        return Response(source.public(), status=status.HTTP_200_OK)


class _Switch(_Base):
    on = False

    def post(self, request, item_id: int):
        item = self._item(request, item_id)
        if item is None or inbox_ai.source_of(request.user, item) is None:
            return _not_found()
        try:
            return Response(inbox_ai.switch(request.user, item, self.on), status=status.HTTP_200_OK)
        except inbox_ai.NothingToSwitch as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except schedules.ScheduleError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class InboxAiOffView(_Switch):
    on = False


class InboxAiOnView(_Switch):
    on = True
