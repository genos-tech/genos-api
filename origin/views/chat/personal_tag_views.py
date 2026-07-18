"""Personal (per-user, PRIVATE) tags on GM channels.

Serves the sidebar tag feature end-to-end:

  GET/POST  /api/v3/personal-tags/                       bundle / create
  PATCH/DEL /api/v3/personal-tags/{tag_id}/              rename / recolor / pin / delete
  PUT       /api/v3/channels/{id}/personal-tags/         replace a channel's tag set

Everything operates strictly on `request.user`'s own tags. Tags are
NEVER exposed through `ChannelSerializer` (see the model docstring for
the sockets-broadcast leak this prevents) — this module is the only
read/write surface. Cross-user tag ids 404 (not 403) so tag existence
doesn't leak, mirroring `_get_channel_for_user`.

Wire format is camelCase (v3 house style), payloads are hand-rolled
dicts like the user-preference views — too small for a serializer.
"""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.personal_tag_models import (
    PersonalChannelTag,
    PersonalChannelTagAssignment,
)
from origin.models.chat.unified_models import ChannelKind, Message
from origin.views.chat.channel_views import _get_channel_for_user
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

MAX_TAGS_PER_USER = 50
MAX_TAGS_PER_CHANNEL = 20
MAX_NAME_LENGTH = 30
# Default-chip ranking: how many chips the recency fallback yields, and
# the bounds on the "my recent GM sends" scan that feeds it.
DEFAULT_VISIBLE_CAP = 6
RECENT_RESPONSE_DAYS = 30
RECENT_RESPONSE_SCAN_LIMIT = 500


def _tag_dict(tag):
    return {
        "tagId": tag.tag_id,
        "name": tag.name,
        "color": tag.color,
        "textColor": tag.text_color,
        "isDefaultVisible": tag.is_default_visible,
        "sortOrder": tag.sort_order,
    }


def _validate_name(request, exclude_tag_id=None):
    """Return (name, error_response). Name errors are 400s."""
    name = (request.data.get("name") or "").strip()
    if not name:
        return None, Response({"error": "name is required."}, status=status.HTTP_400_BAD_REQUEST)
    if len(name) > MAX_NAME_LENGTH:
        return None, Response(
            {"error": f"name must be {MAX_NAME_LENGTH} characters or fewer."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    # The DB unique constraint is case-sensitive; this iexact check is
    # what keeps "Client" / "client" from coexisting. A concurrent-create
    # race falls through to the constraint (caught as IntegrityError).
    dupes = PersonalChannelTag.objects.filter(user=request.user, name__iexact=name)
    if exclude_tag_id is not None:
        dupes = dupes.exclude(tag_id=exclude_tag_id)
    if dupes.exists():
        return None, Response(
            {"error": "A tag with this name already exists."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return name, None


def _recent_gm_channel_ids(user):
    """Channel ids of GMs the user recently sent messages in, most
    recent first. Bounded scan over msg_sender_ts_idx — proportional to
    the user's own sends, never to channel volume."""
    cutoff = timezone.now() - timedelta(days=RECENT_RESPONSE_DAYS)
    recent = Message.objects.filter(
        sender=user,
        ts_sent_at__gte=cutoff,
        deleted_at__isnull=True,
        channel__kind=ChannelKind.GM,
        channel__is_deleted=False,
    ).order_by("-ts_sent_at")
    seen = set()
    ordered = []
    for channel_id in recent.values_list("channel_id", flat=True)[:RECENT_RESPONSE_SCAN_LIMIT]:
        if channel_id not in seen:
            seen.add(channel_id)
            ordered.append(channel_id)
    return ordered


class PersonalTagListView(AuthenticatedAPIView):
    """GET the whole sidebar bundle in one round-trip; POST a new tag."""

    def get(self, request):
        tags = list(
            PersonalChannelTag.objects.filter(user=request.user).order_by("sort_order", "name")
        )

        # Assignments joined to ACTIVE membership + live channel, so GMs
        # the user left (or that were soft-deleted) never surface stale
        # channel ids. The rows themselves are kept — rejoining a GM
        # resurrects its tags, which is the desired behavior.
        assignment_rows = PersonalChannelTagAssignment.objects.filter(
            tag__user=request.user,
            channel__is_deleted=False,
            channel__members__user=request.user,
            channel__members__is_deleted=False,
        ).values_list("channel_id", "tag_id")
        assignments = {}
        tags_by_channel_order = {}  # preserves per-channel insertion order for ranking
        for channel_id, tag_id in assignment_rows:
            assignments.setdefault(str(channel_id), []).append(tag_id)
            tags_by_channel_order.setdefault(channel_id, []).append(tag_id)

        # Default-visible chips: the user's pinned set when they have
        # customized (>=1 pinned), else recency-derived (tags of GMs
        # they recently responded in), capped.
        pinned = [t.tag_id for t in tags if t.is_default_visible]
        if pinned:
            default_visible = pinned
        else:
            default_visible = []
            seen = set()
            for channel_id in _recent_gm_channel_ids(request.user):
                for tag_id in tags_by_channel_order.get(channel_id, []):
                    if tag_id not in seen:
                        seen.add(tag_id)
                        default_visible.append(tag_id)
                        if len(default_visible) >= DEFAULT_VISIBLE_CAP:
                            break
                if len(default_visible) >= DEFAULT_VISIBLE_CAP:
                    break

        return Response(
            {
                "tags": [_tag_dict(t) for t in tags],
                "assignments": assignments,
                "defaultVisibleTagIds": default_visible,
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        name, err = _validate_name(request)
        if err is not None:
            return err
        if PersonalChannelTag.objects.filter(user=request.user).count() >= MAX_TAGS_PER_USER:
            return Response(
                {"error": f"Tag limit reached ({MAX_TAGS_PER_USER})."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        color = (request.data.get("color") or "").strip()[:10]
        text_color = (request.data.get("textColor") or "").strip()[:10]
        if not color or not text_color:
            return Response(
                {"error": "color and textColor are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            tag = PersonalChannelTag.objects.create(
                user=request.user, name=name, color=color, text_color=text_color
            )
        except IntegrityError:
            return Response(
                {"error": "A tag with this name already exists."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(_tag_dict(tag), status=status.HTTP_201_CREATED)


class PersonalTagDetailView(AuthenticatedAPIView):
    """PATCH / DELETE one of the calling user's own tags."""

    def _get_owned(self, request, tag_id):
        """Owned tag or None. Callers 404 (never 403) on None so tag
        existence doesn't leak across users."""
        return PersonalChannelTag.objects.filter(user=request.user, tag_id=tag_id).first()

    def patch(self, request, tag_id):
        tag = self._get_owned(request, tag_id)
        if tag is None:
            return Response({"error": "Tag not found."}, status=status.HTTP_404_NOT_FOUND)

        if "name" in request.data:
            name, err = _validate_name(request, exclude_tag_id=tag.tag_id)
            if err is not None:
                return err
            tag.name = name
        if "color" in request.data:
            tag.color = (request.data.get("color") or "").strip()[:10]
        if "textColor" in request.data:
            tag.text_color = (request.data.get("textColor") or "").strip()[:10]
        if "isDefaultVisible" in request.data:
            value = request.data.get("isDefaultVisible")
            if not isinstance(value, bool):
                return Response(
                    {"error": "isDefaultVisible must be a boolean."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            tag.is_default_visible = value
        if "sortOrder" in request.data:
            try:
                tag.sort_order = int(request.data.get("sortOrder"))
            except (TypeError, ValueError):
                return Response(
                    {"error": "sortOrder must be an integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        try:
            tag.save()
        except IntegrityError:
            return Response(
                {"error": "A tag with this name already exists."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(_tag_dict(tag), status=status.HTTP_200_OK)

    def delete(self, request, tag_id):
        tag = self._get_owned(request, tag_id)
        if tag is None:
            return Response({"error": "Tag not found."}, status=status.HTTP_404_NOT_FOUND)
        tag.delete()  # assignments CASCADE
        return Response(status=status.HTTP_204_NO_CONTENT)


class ChannelPersonalTagsView(AuthenticatedAPIView):
    """PUT the full tag set for one GM channel (replace-set semantics —
    matches a checkbox UI, idempotent, no add/remove race)."""

    def put(self, request, channel_id):
        # Active member or 404 (no channel-existence leak).
        channel = _get_channel_for_user(channel_id, request.user)
        if channel.kind != ChannelKind.GM:
            return Response(
                {"error": "Personal tags are only supported on GM channels."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tag_ids = request.data.get("tagIds")
        if not isinstance(tag_ids, list) or not all(isinstance(t, int) for t in tag_ids):
            return Response(
                {"error": "tagIds must be a list of integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        tag_ids = list(dict.fromkeys(tag_ids))  # dedupe, keep order
        if len(tag_ids) > MAX_TAGS_PER_CHANNEL:
            return Response(
                {"error": f"A channel can carry at most {MAX_TAGS_PER_CHANNEL} tags."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        owned = set(
            PersonalChannelTag.objects.filter(user=request.user, tag_id__in=tag_ids).values_list(
                "tag_id", flat=True
            )
        )
        if len(owned) != len(tag_ids):
            return Response(
                {"error": "One or more tagIds do not exist."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            PersonalChannelTagAssignment.objects.filter(
                tag__user=request.user, channel=channel
            ).exclude(tag_id__in=tag_ids).delete()
            existing = set(
                PersonalChannelTagAssignment.objects.filter(
                    tag__user=request.user, channel=channel
                ).values_list("tag_id", flat=True)
            )
            PersonalChannelTagAssignment.objects.bulk_create(
                [
                    PersonalChannelTagAssignment(tag_id=tag_id, channel=channel)
                    for tag_id in tag_ids
                    if tag_id not in existing
                ],
                ignore_conflicts=True,
            )

        return Response(
            {"channelId": str(channel.id), "tagIds": tag_ids},
            status=status.HTTP_200_OK,
        )
