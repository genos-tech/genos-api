"""
v3 activity feed endpoints.

  GET    /api/v3/activities/?since=ISO&team_id=   — list activities for the
                                                    requesting user, newest
                                                    first. `since` is an
                                                    optional ISO 8601 cutoff;
                                                    omitted ⇒ last 30 days.
                                                    `team_id` narrows to one
                                                    team — a user in two teams
                                                    must not see one team's
                                                    feed while in the other.
  PUT    /api/v3/activities/{id}/read/             — mark one entry read.
  PUT    /api/v3/activities/read-all/              — mark every entry the
                                                    user can see read
                                                    (optionally scoped to
                                                    one channel).
  PUT    /api/v3/activities/read-batch/            — mark a specific set of
                                                    activity ids read.

Replaces the legacy `/api/v2/chat/activity/...` surface that lived on
the `ActivityFact` model (now deleted). FE reads from this directly
and also receives live `activity.created` events over the v3 WS.
"""

import base64
import binascii
import json
import uuid
from datetime import datetime, timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import Activity
from origin.serializers.chat.unified_serializers import ActivitySerializer
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.incremental import capture_server_time
from origin.views.utils.scope_guards import is_team_member

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


def _encode_cursor(row) -> str:
    """An opaque position in the newest-first order: the last row returned.

    `ts_created_at` alone is not enough: rows can share a timestamp, and a
    cursor on it would skip or repeat the ones at the boundary. `id` breaks
    the tie, so `(ts_created_at, id)` is a total order. Opaque (base64 JSON)
    so a client doesn't depend on its shape.
    """
    raw = json.dumps({"t": row.ts_created_at.isoformat(), "i": str(row.id)})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(raw):
    """`(datetime, uuid string)` or `None` for absent OR unreadable input.

    A cursor that doesn't decode is treated as no cursor: the first page,
    never a 500 and never a page that silently starts somewhere else.
    """
    if not raw:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(str(raw).encode()))
        when = datetime.fromisoformat(data["t"])
        if timezone.is_naive(when):
            when = timezone.make_aware(when)
        return when, str(uuid.UUID(str(data["i"])))
    except (ValueError, KeyError, TypeError, binascii.Error, json.JSONDecodeError):
        return None


class ActivityListView(AuthenticatedAPIView):
    """GET /api/v3/activities/?since=ISO&team_id=UUID

    `?team_id=` narrows to one team, and the sidebar now sends it.

    It used to pass nothing, which meant a user who belongs to two teams
    got both teams' entries in one list — team A's mentions and replies
    showed up while team B was on screen. An `Activity` belongs to
    exactly one team (`Activity.team` is non-null, surface mentions
    included), so narrowing is all it takes. Same fix, same reasoning as
    `ChannelListView`, which the chat list already relies on; unnarrowed
    is the compatibility path rather than the intended one.

    **A page is the `_MAX_ROWS` newest rows, and the response says so.** It
    used to cut at 200 silently, and the client stores `server_time` as its
    next `since`, so a user with more than 200 entries lost the rest for good
    (on a full load it wiped the store first). `has_more` says there is an
    older page; `next_cursor` is the position to pass back as `?cursor=` to
    get it. A request with no `cursor` is unchanged, so an old client keeps
    working and still gets the newest page (MIP-740).
    """

    def get(self, request):
        # BEFORE any query (views/utils/incremental.py): the client stores this
        # as its next `since`, so a row that commits while the query runs must
        # be stamped later than it. Read after the query, as this was, a row
        # landing in between is missing from this response AND older than the
        # checkpoint, so no later sync fetches it (MIP-804).
        server_time = capture_server_time()
        since = _parse_since(request.GET.get("since"))
        if since is None:
            since = timezone.now() - timedelta(days=_DEFAULT_SINCE_DAYS)
        team_id = request.GET.get("team_id")
        if team_id:
            # Parse before filtering. `Activity.team` points at a UUID
            # column, so `filter(team_id="abc")` raises ValidationError
            # out of the ORM — a 500 on request input, reachable by
            # anyone now that the sidebar sends this on every load. An
            # unparseable id names no team, which is the same answer as
            # a team you have no entries in.
            try:
                team_id = str(uuid.UUID(str(team_id)))
            except (ValueError, AttributeError, TypeError):
                return Response(
                    {
                        "activities": [],
                        "since": since.isoformat(),
                        "server_time": server_time.isoformat(),
                    }
                )
        # The team narrowing has to land BEFORE the `[:_MAX_ROWS]` slice
        # below — a filter on a sliced queryset raises.
        narrowing = {"team_id": team_id} if team_id else {}
        qs = (
            Activity.objects.filter(
                recipient=request.user,
                ts_created_at__gte=since,
                **narrowing,
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
            .order_by("-ts_created_at", "-id")
        )
        position = _decode_cursor(request.GET.get("cursor"))
        if position is not None:
            when, row_id = position
            # Strictly older in the (ts_created_at, id) order.
            qs = qs.filter(Q(ts_created_at__lt=when) | Q(ts_created_at=when, id__lt=row_id))
        # One past the page, to know whether there is another without a COUNT.
        rows = list(qs[: _MAX_ROWS + 1])
        has_more = len(rows) > _MAX_ROWS
        rows = rows[:_MAX_ROWS]
        return Response(
            {
                "activities": ActivitySerializer(rows, many=True).data,
                "since": since.isoformat(),
                "server_time": server_time.isoformat(),
                "has_more": has_more,
                "next_cursor": _encode_cursor(rows[-1]) if has_more else None,
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


class ActivityReadBatchView(AuthenticatedAPIView):
    """Mark a specific SET of activities read in one request.

    Powers the sidebar's "mark all *currently-filtered* activities read"
    action, where the visible set is a client-side filter (chip / instance
    / mention-group / unread) that spans multiple channels — and
    channel-less surfaces like note mentions — so neither the per-id
    `read/` nor the per-channel `read-all/` endpoint can express it.

    Requires a non-empty `activity_ids` list and 400s otherwise, so it can
    never silently degrade into the "mark every unread entry" default of
    `read-all/`. Scoped to the requesting user so a stray id can't flip
    someone else's row.
    """

    def put(self, request):
        activity_ids = (request.data or {}).get("activity_ids")
        if not activity_ids or not isinstance(activity_ids, list):
            return Response(
                {"error": "activity_ids must be a non-empty list."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        updated = Activity.objects.filter(
            id__in=activity_ids, recipient=request.user, is_read=False
        ).update(is_read=True)
        return Response({"updated": updated}, status=status.HTTP_200_OK)


class ActivitySurfaceView(AuthenticatedAPIView):
    """Create / delete channel-less "surface" mention activities — task
    body + note @-mentions (legacy chat_type 5=task body, 6=personal
    note, 7=task note, 8=chat note).

    Replaces the deleted `PUT /api/v2/chat/activity/` persist that the
    Flask `task_body_mention` / `note_mention` handlers used to call
    (which wrote the now-dropped `ActivityFact`). This writes v3
    `Activity` rows instead and returns the created rows so the WS layer
    can broadcast `activity.created`.

    Delta-driven: `newly_mentioned_user_ids` each get a row;
    `removed_user_ids` have theirs deleted (so clearing a mention from a
    body removes the recipient's feed entry). The mentioner is the
    authenticated `request.user`.

    Body:
      surface_type:             5|6|7|8
      team_id:                  uuid
      entity_key:               stable per-entity key, e.g. "task:123" / "note:2:45"
      newly_mentioned_user_ids: [uuid]
      removed_user_ids:         [uuid]
      meta:                     routing fields the FE reads (taskId/projectId/noteId/chatId/…)
    """

    def post(self, request):
        from origin.services import v3_activity

        data = request.data or {}
        surface_type = data.get("surface_type")
        team_id = data.get("team_id")
        entity_key = data.get("entity_key")
        if surface_type is None or not team_id or not entity_key:
            return Response(
                {"error": "surface_type, team_id and entity_key are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # The caller (the mentioner / `actor`) must belong to the team
        # they're creating activities in — otherwise an authenticated user
        # could fabricate sidebar entries for any member of any team whose
        # id they know. (Recipient ids are separately validated against
        # team membership inside the producer.) `is_team_member`, not a
        # bare TeamMembers query, so a team owner without a member row
        # isn't refused in their own team.
        if not is_team_member(team_id, request.user.id):
            return Response(
                {"error": "Not a member of this team."},
                status=status.HTTP_403_FORBIDDEN,
            )
        rows = v3_activity.create_surface_mention_activities(
            team_id=team_id,
            actor=request.user,
            surface_type=int(surface_type),
            entity_key=str(entity_key),
            newly_mentioned_user_ids=data.get("newly_mentioned_user_ids") or [],
            removed_user_ids=data.get("removed_user_ids") or [],
            meta=data.get("meta") or {},
        )
        # Web Push for the away recipients of these surface (task-body /
        # note) mentions. `rows` is only the genuinely-new mentions, so a
        # repeated body save doesn't re-push.
        from origin.services.webpush_dispatch import schedule_push_for_activities

        schedule_push_for_activities(rows)
        return Response(
            {"activities": ActivitySerializer(rows, many=True).data},
            status=status.HTTP_201_CREATED,
        )


class ActivityCommentSurfaceView(AuthenticatedAPIView):
    """Create "someone commented" surface activities for a comment left on a
    task body / note — the general-comment fan-out (surface owner +
    stakeholders hear about a comment even when it @-mentions no one).

    The comment-participant twin of `ActivitySurfaceView`. Where that one
    takes an explicit recipient delta (the mention set), this one resolves
    recipients server-side from the authoritative id on the surface, so the
    body carries no recipient list — only the surface coordinates in `meta`
    and the `mentioned_user_ids` to EXCLUDE (they get the more-specific
    MENTION row from the mention endpoint).

    Body:
      surface_type:        5|6|7|8
      team_id:             uuid
      entity_key:          per-comment key, e.g. "comment-participant:<id>"
      meta:                routing fields (taskId/projectId/noteId/chatId/…)
      mentioned_user_ids:  [uuid]  — excluded from the participant fan-out
    """

    def post(self, request):
        from origin.services import v3_activity

        data = request.data or {}
        surface_type = data.get("surface_type")
        team_id = data.get("team_id")
        entity_key = data.get("entity_key")
        if surface_type is None or not team_id or not entity_key:
            return Response(
                {"error": "surface_type, team_id and entity_key are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Same actor-membership guard as the mention endpoint: the commenter
        # must belong to the team, so an authenticated user can't fabricate
        # activities in a team they're not in. (Recipients are separately
        # validated against team membership inside the producer.)
        if not is_team_member(team_id, request.user.id):
            return Response(
                {"error": "Not a member of this team."},
                status=status.HTTP_403_FORBIDDEN,
            )
        rows = v3_activity.create_surface_comment_activities(
            team_id=team_id,
            actor=request.user,
            surface_type=int(surface_type),
            entity_key=str(entity_key),
            meta=data.get("meta") or {},
            mentioned_user_ids=data.get("mentioned_user_ids") or [],
        )
        from origin.services.webpush_dispatch import schedule_push_for_activities

        schedule_push_for_activities(rows)
        return Response(
            {"activities": ActivitySerializer(rows, many=True).data},
            status=status.HTTP_201_CREATED,
        )
