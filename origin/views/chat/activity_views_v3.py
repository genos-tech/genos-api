"""
v3 activity feed endpoints.

  GET    /api/v3/activities/?since=ISO            — list activities for the
                                                    requesting user, newest
                                                    first. `since` is an
                                                    optional ISO 8601 cutoff;
                                                    omitted ⇒ last 30 days.
  PUT    /api/v3/activities/{id}/read/             — mark one entry read.
  PUT    /api/v3/activities/read-all/              — mark every entry the
                                                    user can see read.

Replaces the legacy `/api/v2/chat/activity/...` surface that lived on
the `ActivityFact` model (now deleted). FE reads from this directly
and also receives live `activity.created` events over the v3 WS.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import Activity
from origin.serializers.chat.unified_serializers import ActivitySerializer
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

_DEFAULT_SINCE_DAYS = 30
_MAX_ROWS = 200


def _parse_since(raw):
    """`since` accepts an ISO 8601 datetime (`?since=2026-05-30T00:00Z`)
    or a date (`?since=2026-05-30`). Returns a tz-aware datetime or
    `None` if the param was absent/blank — callers fall back to a
    default lookback window when None."""
    if not raw:
        return None
    cleaned = str(raw).strip().replace("Z", "+00:00")
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


class ActivityListView(AuthenticatedAPIView):
    def get(self, request):
        since = _parse_since(request.GET.get("since"))
        if since is None:
            since = timezone.now() - timedelta(days=_DEFAULT_SINCE_DAYS)
        qs = (
            Activity.objects.filter(
                recipient=request.user,
                ts_created_at__gte=since,
            )
            .select_related(
                "actor",
                "channel",
                "channel__team",
                "channel__project",
                "message",
                "message__sender",
                "message__channel",
                "message__task",
                "message__task__project",
            )
            .prefetch_related(
                "message__reactions__user",
                "message__mentions",
                "message__attachments",
            )
            .order_by("-ts_created_at")[:_MAX_ROWS]
        )
        return Response(
            {
                "activities": ActivitySerializer(qs, many=True).data,
                "since": since.isoformat(),
                "server_time": timezone.now().isoformat(),
            }
        )


class ActivityReadView(AuthenticatedAPIView):
    """Single-row read flip. Scoped to the requesting user so a stray
    activity id can't be flipped by someone else."""

    def put(self, request, activity_id):
        updated = Activity.objects.filter(
            id=activity_id, recipient=request.user, is_read=False
        ).update(is_read=True)
        if updated == 0:
            # Either the row doesn't exist, doesn't belong to us, or
            # was already read. 200 in all cases — the operation is
            # idempotent from the caller's perspective.
            return Response({"updated": 0}, status=status.HTTP_200_OK)
        return Response({"updated": updated}, status=status.HTTP_200_OK)


class ActivityReadAllView(AuthenticatedAPIView):
    """Bulk mark-as-read.

    Defaults to "every unread entry the user owns" — the sidebar's
    catch-all button. Pass `?channel_id=<uuid>` to scope to a single
    channel (replaces the legacy `/chat/activity/read/all/` endpoint's
    `chat_type` + `chat_id` body).
    """

    def put(self, request):
        qs = Activity.objects.filter(recipient=request.user, is_read=False)
        channel_id = request.GET.get("channel_id") or (request.data or {}).get("channel_id")
        if channel_id:
            qs = qs.filter(channel_id=channel_id)
        updated = qs.update(is_read=True)
        return Response({"updated": updated}, status=status.HTTP_200_OK)
