"""
Channel-level REST endpoints for the unified messaging schema.

`/api/v3/channels/` — list user's channels (chat list).
`/api/v3/channels/{id}/` — single channel detail.

Message-level endpoints (delta sync, send, edit, react) live in
`message_views.py`. Channel creation (DM/GM/MDM-specific create flows)
will live here in a follow-up commit.

Permissions model: every read/write is scoped to channels the requesting
user is an active ChannelMember of. The `_get_channel_for_user` helper
both fetches the channel AND verifies membership in one indexed query —
if the user isn't a member, the response is 404 (not 403) so we don't
leak channel existence.
"""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.http import Http404
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from origin.models.chat.unified_models import (
    Channel,
    ChannelDirectPair,
    ChannelKind,
    ChannelMember,
    Message,
    ReadCursor,
)
from origin.models.common.team_models import TeamMaster
from origin.serializers.chat.unified_serializers import (
    ChannelMemberSerializer,
    ChannelSerializer,
    MessageSerializer,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

User = get_user_model()


def _canonical_dm_pair(user_a_id, user_b_id):
    """Return (user_lo, user_hi) in canonical order for ChannelDirectPair.

    The pair is order-insensitive: a DM between A and B is the same
    channel regardless of which user the request came from. We sort the
    UUIDs as strings so the canonicalization is deterministic.
    """
    a, b = str(user_a_id), str(user_b_id)
    return (a, b) if a < b else (b, a)


def _verify_team_member(user, team_id):
    """Return TeamMaster iff the user is a team member; else 404.

    Channel create needs both (a) the team exists and (b) the requesting
    user is allowed to create channels in that team. The legacy
    DM/GM/MDM views did this check implicitly via the team FK plus
    membership tables; the unified view centralizes it.
    """
    try:
        # `team_members` is the reverse accessor on TeamMembers; `attendee`
        # is the FK field on that table. The legacy code uses this
        # same pair throughout (e.g. ProjectMembers tracks `attendee`,
        # not `user`).
        return TeamMaster.objects.get(
            team_id=team_id,
            team_members__attendee=user,
            team_members__is_deleted=False,
        )
    except TeamMaster.DoesNotExist:
        # Could be the team doesn't exist OR user isn't a member. We
        # don't distinguish — 404 in either case.
        raise Http404("Team not found.")


def _user_channels_qs(user):
    """Active channels the given user is a member of, with related fields
    needed for serialization preloaded."""
    return (
        Channel.objects.filter(
            members__user=user,
            members__is_deleted=False,
            is_deleted=False,
        )
        .select_related("project", "team", "owner")
        .distinct()
    )


def _get_channel_for_user(channel_id, user):
    """Fetch one channel by id, scoped to the user's membership.

    Returns the Channel or raises Http404 — never 403, so we don't leak
    the existence of channels the user can't see.
    """
    try:
        return _user_channels_qs(user).get(id=channel_id)
    except Channel.DoesNotExist:
        raise Http404("Channel not found.")


def _annotate_unread(qs, user):
    """Attach `_unread_count` to each channel in qs: the EXACT count of
    top-level, non-deleted messages whose seq is beyond the user's
    main-timeline read cursor. A single correlated COUNT subquery (NOT
    N+1). `seq` is allocated per channel across thread replies AND
    soft-deleted rows, so a `latest_seq - read_seq` difference would
    over-count — hence the explicit COUNT.
    """
    # The user's main-timeline read cursor seq for the (inner) message's
    # channel. `OuterRef("channel")` correlates to the unread subquery's
    # Message row (one level out), NOT the Channel (two levels out).
    read_cursor_seq = ReadCursor.objects.filter(
        user=user,
        channel=OuterRef("channel"),
        thread_root__isnull=True,
    ).values("last_read_message__seq")[:1]

    unread_count = (
        Message.objects.filter(
            channel=OuterRef("pk"),
            is_thread_reply=False,
            deleted_at__isnull=True,
            seq__gt=Coalesce(Subquery(read_cursor_seq), 0),
        )
        .order_by()
        .values("channel")
        .annotate(c=Count("id"))
        .values("c")[:1]
    )
    return qs.annotate(_unread_count=Coalesce(Subquery(unread_count), 0))


def _annotate_latest_and_unread(qs, user):
    """`_annotate_unread` + `_latest_seq` (the highest top-level
    non-deleted seq per channel, used to resolve `latestMessage`).

    Only ChannelDetailView needs `_latest_seq`. The LIST view resolves
    `latestMessage` via a single Postgres DISTINCT ON query (see
    ChannelListView.get) and so calls `_annotate_unread` directly,
    skipping this per-channel correlated `_latest_seq` subquery.
    """
    latest_seq = (
        Message.objects.filter(
            channel=OuterRef("pk"),
            is_thread_reply=False,
            deleted_at__isnull=True,
        )
        .order_by("-seq")
        .values("seq")[:1]
    )
    return _annotate_unread(qs, user).annotate(_latest_seq=Subquery(latest_seq))


class ChannelListView(AuthenticatedAPIView):
    """GET /api/v3/channels/

    Returns the requesting user's chat list (all kinds: DM/GM/PM/MDM
    mixed). The client sorts by `latestMessage.tsSent` desc in-memory;
    the API returns them in deterministic id order for cache stability.

    Each row carries a denormalized `latestMessage` and `unreadCount` so
    the chat-list sidebar renders in a single round-trip.
    """

    def get(self, request):
        user = request.user
        # Only the unread COUNT subquery is needed here — `latestMessage`
        # is resolved below by a single DISTINCT ON query, so we skip the
        # per-channel `_latest_seq` correlated subquery entirely.
        qs = _annotate_unread(_user_channels_qs(user), user)

        channels = list(qs)
        if not channels:
            return Response({"channels": []})

        # Latest non-thread, non-deleted message per channel in ONE query.
        # Postgres DISTINCT ON (channel_id) ordered by (channel_id, -seq)
        # keeps the highest-seq row per channel — replacing the previous
        # N-term `Q(channel_id=, seq=) | …` OR chain (one OR branch per
        # channel, which degraded linearly with channel count). The
        # (channel, seq) unique constraint guarantees exactly one row per
        # channel (no tie ambiguity), and the filters mirror `_latest_seq`
        # exactly (top-level, non-deleted) so the resolved message is
        # identical to what the OR chain returned.
        ids = [c.id for c in channels]
        latest_messages = (
            Message.objects.filter(
                channel_id__in=ids,
                is_thread_reply=False,
                deleted_at__isnull=True,
            )
            .order_by("channel_id", "-seq")
            .distinct("channel_id")
            .select_related("sender", "channel", "task", "task__project")
            .prefetch_related("reactions__user", "mentions", "attachments")
        )
        latest_by_channel = {m.channel_id: m for m in latest_messages}

        # Attach the latest message. `_unread_count` is annotated by
        # `_annotate_unread` (correlated COUNT subquery), so the serializer
        # reads it directly — no per-row computation here.
        for c in channels:
            c._latest_message = latest_by_channel.get(c.id)

        # Attach members for DM/MDM rows only (DM partner identity + MDM
        # avatars are resolved client-side from this roster). One batched
        # query for all such channels — NOT a blanket prefetch, so large
        # GM rosters never ride this hot path. GM/PM rows render from
        # `title` and get an empty roster.
        dm_mdm_ids = [c.id for c in channels if c.kind in (ChannelKind.DM, ChannelKind.MDM)]
        members_by_channel: dict = {}
        if dm_mdm_ids:
            for m in (
                ChannelMember.objects.filter(channel_id__in=dm_mdm_ids, is_deleted=False)
                .select_related("user")
                .order_by("ts_joined_at")
            ):
                members_by_channel.setdefault(m.channel_id, []).append(m)
        for c in channels:
            c.active_members = members_by_channel.get(c.id, [])

        data = ChannelSerializer(channels, many=True, context={"request": request}).data
        return Response({"channels": data})

    def post(self, request):
        """POST /api/v3/channels/

        Create a new DM/GM/MDM channel. PM channel creation is NOT
        exposed here — PM channels are 1:1 with ProjectMaster and get
        auto-created by a Django signal when a project is created.

        Request body:
            {
              "kind": 1|2|4,                     # DM=1, GM=2, MDM=4
              "team_id": "<team_uuid>",
              "title": "<str>" (GM/MDM only),
              "is_private": <bool> (GM only, default false),
              "profile_image_url": "<str>" (GM only, optional),
              "other_user_id": "<uuid>" (DM only — the other party),
              "member_user_ids": ["<uuid>", ...] (GM/MDM only — initial members
                                                 excluding the creator)
            }

        For DM: if a channel already exists between the requester and
        `other_user_id`, returns it instead of creating a duplicate
        (idempotent — important because the FE can hit this endpoint
        before knowing whether the DM already exists).
        """
        user = request.user
        body = request.data or {}
        kind = body.get("kind")
        team_id = body.get("team_id")

        if kind not in (ChannelKind.DM, ChannelKind.GM, ChannelKind.MDM):
            return Response(
                {"error": "kind must be 1 (DM), 2 (GM), or 4 (MDM)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        team = _verify_team_member(user, team_id)

        if kind == ChannelKind.DM:
            return self._create_dm(request, team, body)
        else:
            return self._create_group(request, team, kind, body)

    @staticmethod
    def _create_dm(request, team, body):
        """DM-specific create. Idempotent via ChannelDirectPair."""
        other_user_id = body.get("other_user_id")
        if not other_user_id:
            return Response(
                {"error": "other_user_id is required for DM creation."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if str(other_user_id) == str(request.user.id):
            return Response(
                {"error": "Cannot create a DM with yourself."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            other = User.objects.get(id=other_user_id)
        except User.DoesNotExist:
            return Response(
                {"error": "other_user_id not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        user_lo, user_hi = _canonical_dm_pair(request.user.id, other.id)

        # Idempotent lookup — if a DM already exists for this pair AND
        # the requester is a current member, return it.
        with transaction.atomic():
            existing = (
                ChannelDirectPair.objects.select_related("channel")
                .filter(user_lo=user_lo, user_hi=user_hi)
                .first()
            )
            if existing:
                # `uniq_dm_pair` makes (user_lo, user_hi) map to exactly
                # ONE channel for the pair's lifetime. Reuse it — REGARDLESS
                # of `is_deleted` — instead of creating a second channel: a
                # `ChannelDirectPair.create` for the same pair would violate
                # the unique constraint (→ 500), and even without it the DM
                # history would split across two channel UUIDs. Reactivating
                # the existing channel preserves the conversation.
                channel = existing.channel
                if channel.is_deleted:
                    channel.is_deleted = False
                    channel.save(update_fields=["is_deleted"])
                # Re-activate the requester's membership if they had
                # left/been removed. The other side's membership is left
                # as-is; if they removed themselves it stays removed.
                ChannelMember.objects.update_or_create(
                    channel=channel,
                    user=request.user,
                    defaults={"is_deleted": False, "role": "member"},
                )
                return Response(
                    {"channel": ChannelSerializer(channel, context={"request": request}).data},
                    status=status.HTTP_200_OK,
                )

            channel = Channel.objects.create(team=team, kind=ChannelKind.DM, title="")
            ChannelDirectPair.objects.create(channel=channel, user_lo=user_lo, user_hi=user_hi)
            ChannelMember.objects.create(channel=channel, user=request.user, role="member")
            ChannelMember.objects.create(channel=channel, user=other, role="member")

        return Response(
            {"channel": ChannelSerializer(channel, context={"request": request}).data},
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _create_group(request, team, kind, body):
        """GM/MDM create. Accepts an arbitrary member list."""
        title = (body.get("title") or "").strip()
        is_private = bool(body.get("is_private", False))
        profile_image_url = body.get("profile_image_url") or ""
        member_user_ids = body.get("member_user_ids") or []

        if kind == ChannelKind.GM and not title:
            return Response(
                {"error": "title is required for GM."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(member_user_ids, list):
            return Response(
                {"error": "member_user_ids must be a list."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate member ids exist; collapse duplicates and drop the
        # creator if accidentally included (we add them separately as
        # owner).
        unique_member_ids = {str(m) for m in member_user_ids if m} - {str(request.user.id)}
        members = list(User.objects.filter(id__in=unique_member_ids)) if unique_member_ids else []
        if len(members) != len(unique_member_ids):
            return Response(
                {"error": "One or more member_user_ids not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        with transaction.atomic():
            channel = Channel.objects.create(
                team=team,
                kind=kind,
                title=title,
                is_private=is_private,
                profile_image_url=profile_image_url,
                owner=request.user,
            )
            ChannelMember.objects.create(channel=channel, user=request.user, role="owner")
            for m in members:
                ChannelMember.objects.create(channel=channel, user=m, role="member")

        return Response(
            {"channel": ChannelSerializer(channel, context={"request": request}).data},
            status=status.HTTP_201_CREATED,
        )


class ChannelDetailView(AuthenticatedAPIView):
    """GET /api/v3/channels/{channel_id}/

    Returns a single channel's metadata + member list. Used when the
    frontend opens a specific channel and needs the title/avatar/member
    list to render the header. Messages are fetched separately via the
    messages-delta endpoint.
    """

    def get(self, request, channel_id):
        channel = _get_channel_for_user(channel_id, request.user)

        # Attach the latest message + unread count for parity with list.
        qs = _annotate_latest_and_unread(Channel.objects.filter(id=channel.id), request.user)
        annotated = qs.first()
        if annotated and annotated._latest_seq is not None:
            latest = (
                Message.objects.filter(
                    channel_id=channel.id,
                    seq=annotated._latest_seq,
                )
                .select_related("sender", "channel", "task", "task__project")
                .prefetch_related("reactions__user", "mentions", "attachments")
                .first()
            )
            channel._latest_message = latest
            # `_unread_count` is the exact correlated-COUNT annotation from
            # `_annotate_latest_and_unread` (no longer a seq difference).
            channel._unread_count = annotated._unread_count
        else:
            channel._latest_message = None
            channel._unread_count = 0

        members = ChannelMember.objects.filter(channel=channel, is_deleted=False).select_related(
            "user"
        )

        return Response(
            {
                "channel": ChannelSerializer(channel, context={"request": request}).data,
                "members": ChannelMemberSerializer(members, many=True).data,
            }
        )

    def patch(self, request, channel_id):
        """Update channel metadata (title, profile image, visibility, owner).

        Authorization: only the channel owner can change metadata.
        DM channels cannot be renamed/customized (their identity is
        the user pair). PM channels have their title/avatar mirrored
        from the underlying ProjectMaster, so updating here would
        desync; return 400 to make the caller go through the project
        edit flow instead.

        Body (any subset, all optional):
            {
              "title": "<str>",              # max 80 chars
              "profile_image_url": "<str>",  # max 512 chars
              "is_private": <bool>,
              "owner_user_id": "<uuid>"      # transfer ownership; the
                                             # target must be a current
                                             # non-deleted member.
            }

        Unknown fields are silently ignored. Empty body returns the
        existing channel unchanged.

        Owner-transfer semantics: setting `owner_user_id` updates the
        channel's `owner` FK AND demotes the requester's
        `ChannelMember.role` to "member" while promoting the target's
        to "owner". The change is atomic — if any step fails the whole
        patch rolls back.
        """
        channel = _get_channel_for_user(channel_id, request.user)
        if channel.kind == ChannelKind.DM:
            return Response(
                {"error": "DM channels cannot be renamed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if channel.kind == ChannelKind.PM:
            return Response(
                {
                    "error": (
                        "PM channels mirror the project's title/avatar; "
                        "edit the project instead."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not channel.owner_id or str(channel.owner_id) != str(request.user.id):
            return Response(
                {"error": "Only the channel owner can edit metadata."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Whitelist patchable fields so a misbehaving client can't write
        # arbitrary columns. `metadata` JSON pass-through would be next
        # to support if a feature ever needs it; punt for now.
        body = request.data or {}
        update_fields = []
        if "title" in body:
            title = (body.get("title") or "").strip()
            if not title:
                return Response(
                    {"error": "title must be a non-empty string."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if len(title) > 80:
                return Response(
                    {"error": "title exceeds 80 chars."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            channel.title = title
            update_fields.append("title")
        if "profile_image_url" in body:
            url = body.get("profile_image_url") or ""
            if len(url) > 512:
                return Response(
                    {"error": "profile_image_url exceeds 512 chars."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            channel.profile_image_url = url
            update_fields.append("profile_image_url")
        if "is_private" in body:
            channel.is_private = bool(body.get("is_private"))
            update_fields.append("is_private")
        new_owner_user_id = body.get("owner_user_id") if "owner_user_id" in body else None
        if "owner_user_id" in body:
            if not new_owner_user_id:
                return Response(
                    {"error": "owner_user_id must be a non-empty user id."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if str(new_owner_user_id) == str(request.user.id):
                return Response(
                    {"error": "owner_user_id matches the current owner — no transfer needed."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                new_owner = User.objects.get(id=new_owner_user_id)
            except (User.DoesNotExist, ValueError):
                return Response(
                    {"error": "owner_user_id not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            # The incoming owner must be a current (non-deleted) member.
            # We don't auto-add them — the caller should run add-member
            # first if needed.
            new_owner_membership = ChannelMember.objects.filter(
                channel=channel,
                user=new_owner,
                is_deleted=False,
            ).first()
            if not new_owner_membership:
                return Response(
                    {"error": "owner_user_id is not a current member of this channel."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            channel.owner = new_owner
            update_fields.append("owner")

        with transaction.atomic():
            if update_fields:
                update_fields.append("ts_updated_at")
                channel.save(update_fields=update_fields)
            # Role swap happens inside the same transaction so the
            # roster never has two owners (or zero) between writes.
            if new_owner_user_id and "owner_user_id" in body:
                ChannelMember.objects.filter(
                    channel=channel,
                    user_id=request.user.id,
                    is_deleted=False,
                ).update(role="member")
                ChannelMember.objects.filter(
                    channel=channel,
                    user_id=new_owner_user_id,
                    is_deleted=False,
                ).update(role="owner")

        return Response({"channel": ChannelSerializer(channel, context={"request": request}).data})


class ChannelMembersView(AuthenticatedAPIView):
    """GET /api/v3/channels/{channel_id}/members/      member roster
    POST /api/v3/channels/{channel_id}/members/      add member(s)

    Per-member removal uses the sibling `ChannelMemberDetailView` at
    `/{channel_id}/members/{user_id}/`. DM channels cannot grow beyond
    the original pair, so POST is rejected for kind=1.
    """

    def get(self, request, channel_id):
        # Membership check + 404 leak prevention via _get_channel_for_user.
        channel = _get_channel_for_user(channel_id, request.user)
        members = ChannelMember.objects.filter(channel=channel, is_deleted=False).select_related(
            "user"
        )
        return Response({"members": ChannelMemberSerializer(members, many=True).data})

    def post(self, request, channel_id):
        """Add one or more members to a GM/MDM channel.

        Request body: {"user_ids": ["<uuid>", ...]}.

        Idempotent: a user already in the channel (active or soft-deleted)
        gets their row re-activated, not duplicated. DM channels return
        400 because their member set is fixed by ChannelDirectPair.
        """
        channel = _get_channel_for_user(channel_id, request.user)
        if channel.kind == ChannelKind.DM:
            return Response(
                {"error": "Cannot add members to a DM channel."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if channel.kind == ChannelKind.PM:
            # PM membership mirrors ProjectMembers via a signal — direct
            # adds are not supported. Keep the 400 explicit so the FE
            # gets a clear error rather than a silent no-op.
            return Response(
                {"error": "PM channel membership is managed via the project."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        body = request.data or {}
        user_ids = body.get("user_ids") or []
        if not isinstance(user_ids, list) or not user_ids:
            return Response(
                {"error": "user_ids must be a non-empty list."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        unique_ids = {str(u) for u in user_ids if u}
        users = list(User.objects.filter(id__in=unique_ids))
        if len(users) != len(unique_ids):
            return Response(
                {"error": "One or more user_ids not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        added = []
        with transaction.atomic():
            for u in users:
                obj, _created = ChannelMember.objects.update_or_create(
                    channel=channel,
                    user=u,
                    defaults={"is_deleted": False, "role": "member"},
                )
                added.append(obj)

        return Response(
            {"members": ChannelMemberSerializer(added, many=True).data},
            status=status.HTTP_201_CREATED,
        )


class ChannelMemberDetailView(AuthenticatedAPIView):
    """DELETE /api/v3/channels/{channel_id}/members/{user_id}/

    Remove a member from a channel (soft-delete the ChannelMember row).
    DM channels reject removal — the pair is fixed; if a user wants to
    "leave" a DM the FE just hides it client-side. PM removal mirrors
    ProjectMembers via a signal, not direct API.

    Authorization: a member can always remove themselves; the channel
    owner can remove anyone. Otherwise 403.
    """

    def delete(self, request, channel_id, user_id):
        channel = _get_channel_for_user(channel_id, request.user)
        if channel.kind == ChannelKind.DM:
            return Response(
                {"error": "Cannot remove members from a DM channel."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if channel.kind == ChannelKind.PM:
            return Response(
                {"error": "PM channel membership is managed via the project."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        is_self = str(user_id) == str(request.user.id)
        is_owner = channel.owner_id and str(channel.owner_id) == str(request.user.id)
        if not (is_self or is_owner):
            return Response(
                {"error": "Only the channel owner can remove other members."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            member = ChannelMember.objects.get(channel=channel, user_id=user_id)
        except ChannelMember.DoesNotExist:
            raise Http404("Member not found.")
        if member.is_deleted:
            # Already gone — 204 anyway for idempotency.
            return Response(status=status.HTTP_204_NO_CONTENT)

        member.is_deleted = True
        member.save(update_fields=["is_deleted", "ts_updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class ChannelProfileImageView(AuthenticatedAPIView):
    """PUT /api/v3/channels/{channel_id}/profile/image/

    Upload a new profile image for a GM/MDM channel. Mirrors the legacy
    `TeamProfileImageView` / `UserProfileImageView` patterns: multipart
    body with a single `profile_image` file field; on success returns
    the updated channel row so callers can read `profile_image_url`
    directly.

    Authorization: only the channel owner can change the avatar. DM
    channels have no avatar (identity is the user pair). PM channels
    mirror the project avatar — edit via the project profile flow.

    Cross-tab refresh: this is a REST endpoint, so it doesn't fan out a
    `channel.updated` socket broadcast. Other open tabs see the new
    avatar on next `listChannels` or `syncChannel` refresh. The caller's
    own tab should invalidate `channelService.snapshot.channels` via
    `syncChannel` after upload — that's the established pattern.
    """

    parser_classes = [MultiPartParser]

    def put(self, request, channel_id):
        channel = _get_channel_for_user(channel_id, request.user)
        if channel.kind == ChannelKind.DM:
            return Response(
                {"error": "DM channels have no avatar."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if channel.kind == ChannelKind.PM:
            return Response(
                {"error": ("PM channels mirror the project avatar; " "edit the project instead.")},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not channel.owner_id or str(channel.owner_id) != str(request.user.id):
            return Response(
                {"error": "Only the channel owner can change the avatar."},
                status=status.HTTP_403_FORBIDDEN,
            )

        profile_image = request.FILES.get("profile_image")
        if profile_image is None:
            return Response(
                {"error": "profile_image is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Django's storage layer resolves the final on-disk path and
        # collision-suffixes the filename if needed. Read `.name` after
        # save to capture whatever it actually stored.
        channel.profile_image_file = profile_image
        channel.save(update_fields=["profile_image_file", "ts_updated_at"])
        channel.profile_image_url = channel.profile_image_file.name
        channel.save(update_fields=["profile_image_url", "ts_updated_at"])

        return Response(
            {"channel": ChannelSerializer(channel, context={"request": request}).data},
            status=status.HTTP_200_OK,
        )
