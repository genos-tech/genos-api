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

import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Count, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.http import Http404
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from origin.models.chat.unified_models import (
    Channel,
    ChannelDirectPair,
    ChannelKind,
    ChannelMember,
    Message,
    ReadCursor,
)
from origin.models.common.team_models import ExternalGrant, TeamMaster
from origin.serializers.chat.unified_serializers import (
    ChannelMemberSerializer,
    ChannelSerializer,
    MessageSerializer,
)
from origin.services.external_grants import (
    ExternalGrantError,
    active_grants_for_object,
    add_external_participants,
    grant_admitting,
    offer_grant,
    remove_external_participants,
    visible_shares_for_object,
)
from origin.services.member_roles import (
    ASSIGNABLE_ROLES,
    OWNER,
    can_manage,
    is_assignable,
    member_role_to_channel_role,
    resolve_gm_role,
    resolve_team_role,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import is_guest, is_team_member, is_team_participant
from origin.views.utils.upload_limits import check_upload_size

User = get_user_model()


def _canonical_dm_pair(user_a_id, user_b_id):
    """Return (user_lo, user_hi) in canonical order for ChannelDirectPair.

    The pair is order-insensitive: a DM between A and B is the same
    channel regardless of which user the request came from. We sort the
    UUIDs as strings so the canonicalization is deterministic.
    """
    a, b = str(user_a_id), str(user_b_id)
    return (a, b) if a < b else (b, a)


def _grant_error_status(exc):
    """Map an `ExternalGrantError` code onto a status code.

    Same 404-never-403 convention as the rest of this module: a caller who
    named an object or team they have no business with is told it is not
    there. `not_a_manager` is a genuine 403 because the caller has already
    proved they belong — hiding that would just be confusing.
    """
    if exc.code in ("not_found", "bad_object", "not_owned", "team_unavailable"):
        return status.HTTP_404_NOT_FOUND
    if exc.code == "not_a_manager":
        return status.HTTP_403_FORBIDDEN
    return status.HTTP_400_BAD_REQUEST


def _channel_admissible(user_id, team_id, channel=None):
    """May this person be a member of this channel?

    Deliberately STRICTER than `is_team_participant`, and the difference
    is the cross-team security boundary. `is_team_participant` says two
    people may be in a room together at all, and now includes members of
    a connected team that shares *something* with this one — right for a
    DM counterparty, far too broad here. Channel membership must be
    grant-bound to THIS channel, or one share would quietly become
    access to every group chat the host owns.

    So: host-side participants (member, owner, guest) always; anyone else
    only with an active `ExternalGrant` naming this exact channel.
    """
    if is_team_member(team_id, user_id) or is_guest(team_id, user_id):
        return True
    if channel is None:
        return False
    return grant_admitting(ExternalGrant.ObjectType.CHANNEL, channel.id, user_id) is not None


def _users_in_team(user_ids, team_id, channel=None):
    """Resolve `user_ids` to users, or `None` if any may not be admitted.

    All-or-nothing on purpose. Silently dropping the ids that don't
    belong would create a channel whose member list quietly differs from
    what the caller asked for — and would leak, by omission, which ids
    were rejected. One 404 for "unknown", "malformed", and "not in this
    team" alike means the endpoint answers nothing about who exists.

    Malformed ids are filtered before the query rather than caught after:
    `id` is a UUIDField, so a bad value raises `ValidationError` out of
    `to_python` and would otherwise be a 500 on attacker-supplied input.

    Pass `channel` when adding to an existing channel: an external chat
    also admits members of a guest team holding an active grant on that
    channel. Without it the check is host-team-only, which is what every
    internal channel wants.
    """
    wanted = {str(uid) for uid in user_ids if uid}
    if not wanted:
        return []
    valid = set()
    for uid in wanted:
        try:
            valid.add(str(uuid.UUID(uid)))
        except (ValueError, AttributeError, TypeError):
            return None
    if len(valid) != len(wanted):
        # Two spellings of the same UUID (case, hyphenation) collapsed —
        # treat as a malformed request rather than silently deduplicating.
        return None

    users = list(User.objects.filter(id__in=valid))
    if len(users) != len(valid):
        return None
    if not all(_channel_admissible(u.id, team_id, channel) for u in users):
        return None
    return users


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


def _get_channel_for_roster_admin(channel_id, user):
    """Like `_get_channel_for_user`, plus the guest team's administrators.

    Needed because of the order things happen in a cross-team share. When
    a guest team accepts a grant, NOBODY from that team is in the channel
    yet — their managers admit their own people afterwards. Scoping the
    roster endpoints to membership alone would leave the guest side with a
    channel they have consented to and cannot staff, and the only way out
    would be the host adding the first person, which is precisely the
    delegation this design removes.

    Widened for *reaching the roster endpoint* only. Who may actually be
    admitted is still `_channel_admissible` plus the grant service, and a
    guest manager still cannot touch the host's own members.
    """
    try:
        return _get_channel_for_user(channel_id, user)
    except Http404:
        pass
    channel = Channel.objects.filter(id=channel_id, is_deleted=False, is_external=True).first()
    if channel is None:
        raise Http404("Channel not found.")
    for grant in active_grants_for_object(ExternalGrant.ObjectType.CHANNEL, channel.id):
        team = TeamMaster.objects.filter(team_id=grant.guest_team_id, is_deleted=False).first()
        if team is not None and can_manage(resolve_team_role(team, user.id)):
            return channel
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

    `?team_id=` narrows to one team, and the chat sidebar now sends it.

    It used to pass nothing, on the reasoning that the sidebar "wants
    every team at once". That was wrong as a product decision: a channel
    belongs to exactly one team (`Channel.team` is non-null, DMs
    included), so switching teams left the previous team's chats on
    screen. Only the webhook scope picker relied on the narrowing before;
    now both callers do, and unnarrowed is the compatibility path rather
    than the intended one.
    """

    def get(self, request):
        user = request.user
        # Only the unread COUNT subquery is needed here — `latestMessage`
        # is resolved below by a single DISTINCT ON query, so we skip the
        # per-channel `_latest_seq` correlated subquery entirely.
        qs = _annotate_unread(_user_channels_qs(user), user)
        team_id = request.GET.get("team_id")
        if team_id:
            # Parse before filtering. `Channel.team` points at a UUID
            # column, so `filter(team_id="abc")` raises ValidationError
            # out of the ORM — a 500 on request input, and reachable by
            # anyone now that the sidebar sends this parameter on every
            # load. An unparseable id names no team, which is the same
            # answer as a team you are not in.
            try:
                team_id = str(uuid.UUID(str(team_id)))
            except (ValueError, AttributeError, TypeError):
                return Response({"channels": []})
            # Narrowing only — `_user_channels_qs` has already restricted
            # this to the caller's own memberships, so naming a team you
            # are not in yields nothing rather than anything new.
            qs = qs.filter(team_id=team_id)

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
        latest_messages = MessageSerializer.annotate_task_comment_count(
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
        """DM-specific create. Idempotent via ChannelDirectPair.

        Supports the **self-DM** (``other_user_id == requester``): the
        personal scratch channel that backs the todo / calendar panes. It
        is normally created on team join, but this path must return /
        recreate it idempotently so that searching your own name in the
        chat search opens it — the frontend can't reliably locate a
        single-member channel in its snapshot. A self-DM has one
        ``ChannelMember`` and a ``ChannelDirectPair`` with
        ``user_lo == user_hi``.

        Note the deliberate asymmetry between the pair and the team: a
        ``ChannelDirectPair`` is globally unique, so two people who share
        *two* teams have one DM channel between them, and it is returned
        whichever team was named in the request. That is intended — a DM
        is between people, not tenants. What both callers must satisfy is
        that they belong to the team named NOW, which is what the caller
        check above and the counterparty check below enforce; someone
        removed from the team can no longer open or re-open the DM.
        """
        other_user_id = body.get("other_user_id")
        if not other_user_id:
            return Response(
                {"error": "other_user_id is required for DM creation."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # A self-DM is allowed (not rejected). `_canonical_dm_pair(self,
        # self)` collapses to (self, self), so the idempotent lookup below
        # finds the existing self-DM, and the create branch makes a
        # single-member channel.
        is_self_dm = str(other_user_id) == str(request.user.id)
        try:
            other = User.objects.get(id=other_user_id)
        except (User.DoesNotExist, DjangoValidationError):
            # ValidationError, not ValueError: `id` is a UUIDField, and a
            # malformed one raises out of `to_python` — uncaught, that is
            # a 500 on attacker-supplied input.
            return Response(
                {"error": "other_user_id not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # The counterparty must belong to this team. Without this, any
        # authenticated user could open a DM with ANY user in the install
        # by id — creating a real channel, with a real ChannelMember row
        # for someone in a different tenant, and a message surface into
        # it. `_verify_team_member` above gates only the *caller*.
        #
        # Same 404 as an unknown id, so "no such user" and "not in your
        # team" are indistinguishable and this cannot be used to probe
        # which user ids exist.
        if not is_team_participant(team.team_id, other.id):
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
            # Self-DM has a single member — a second
            # ChannelMember(channel, request.user) would duplicate the
            # requester's row (and trip the per-channel member uniqueness).
            if not is_self_dm:
                ChannelMember.objects.create(channel=channel, user=other, role="member")

        return Response(
            {"channel": ChannelSerializer(channel, context={"request": request}).data},
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _create_group(request, team, kind, body):
        """GM/MDM create. Accepts an arbitrary member list.

        `is_external: true` (GM only) makes a cross-team chat. It forces
        `is_private` on and takes `guest_team_ids`: connected teams that
        are offered access in the same call, so creating an external chat
        is one round trip rather than create-then-share. Offering is
        owner/editor-only and requires an active connection, both enforced
        by `offer_grant` — creating the empty channel is not, because an
        external chat with no grants shares nothing with anybody.

        `member_user_ids` stays host-side either way. The host puts its own
        people in; each guest team's managers then admit their own, which
        is the whole point of the delegation model and is deliberately not
        something the host can do on their behalf.
        """
        title = (body.get("title") or "").strip()
        is_external = bool(body.get("is_external", False))
        is_private = bool(body.get("is_private", False))
        profile_image_url = body.get("profile_image_url") or ""
        member_user_ids = body.get("member_user_ids") or []
        guest_team_ids = body.get("guest_team_ids") or []

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
        if is_external:
            if kind != ChannelKind.GM:
                return Response(
                    {"error": "Only a group message can be external."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not isinstance(guest_team_ids, list):
                return Response(
                    {"error": "guest_team_ids must be a list."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Never optional. A public external channel could be
            # self-joined by every member of the host team
            # (`ChannelJoinView`), which is access no grant approved — and
            # the guest side would have no way to know it happened.
            is_private = True

        # Validate member ids exist; collapse duplicates and drop the
        # creator if accidentally included (we add them separately as
        # owner).
        unique_member_ids = {str(m) for m in member_user_ids if m} - {str(request.user.id)}
        members = _users_in_team(unique_member_ids, team.team_id)
        if members is None:
            # One or more ids were unknown, malformed, or named somebody
            # outside this team — all the same 404, so the endpoint can't
            # be used to test whether a user id exists.
            return Response(
                {"error": "One or more member_user_ids not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            with transaction.atomic():
                channel = Channel.objects.create(
                    team=team,
                    kind=kind,
                    title=title,
                    is_private=is_private,
                    is_external=is_external,
                    profile_image_url=profile_image_url,
                    owner=request.user,
                )
                ChannelMember.objects.create(channel=channel, user=request.user, role="owner")
                for m in members:
                    ChannelMember.objects.create(channel=channel, user=m, role="member")
                for guest_team_id in {str(g) for g in guest_team_ids if g}:
                    offer_grant(
                        owner_team_id=team.team_id,
                        guest_team_id=guest_team_id,
                        object_type=ExternalGrant.ObjectType.CHANNEL,
                        object_id=channel.id,
                        role_ceiling=body.get("role_ceiling") or "editor",
                        actor=request.user,
                    )
        except ExternalGrantError as exc:
            # Rolled back with the channel: a half-created external chat
            # that named a team it never actually offered access to would
            # look shared and be nothing of the kind.
            return Response({"error": exc.code}, status=_grant_error_status(exc))

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
            latest = MessageSerializer.annotate_task_comment_count(
                Message.objects.filter(
                    channel_id=channel.id,
                    seq=annotated._latest_seq,
                )
                .select_related("sender", "channel", "task", "task__project")
                .prefetch_related("reactions__user", "mentions", "attachments")
            ).first()
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
        from the underlying ProjectMaster, so a direct channel write
        would desync; a `title`-only PM patch instead DELEGATES to the
        project rename (same owner + collision rules as
        `ProjectMasterView.put`) and lets the
        `_ensure_pm_channel_for_project` signal mirror it back. This
        gives PM renames the same live `channel.updated` broadcast
        path as GM renames (the sockets `channel.update` proxy calls
        this view) — without it, other members' sidebars kept the old
        project name until a full reload. Any other field on a PM
        channel is still rejected with "edit the project instead".

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
            return self._patch_pm_title(request, channel)
        # Owner OR editor for ordinary metadata (title / image /
        # visibility). Ownership transfer, handled further down, stays
        # owner-only.
        actor_role = resolve_gm_role(channel, request.user.id)
        if not can_manage(actor_role):
            return Response(
                {"error": "Only the channel owner or an editor can edit metadata."},
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
            next_private = bool(body.get("is_private"))
            if channel.is_external and not next_private:
                # A public external chat would be self-joinable by the
                # whole host team (`ChannelJoinView`), handing out access
                # no grant ever approved — and the guest side would never
                # see it happen.
                return Response(
                    {"error": "An external chat must stay private."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            channel.is_private = next_private
            update_fields.append("is_private")
        new_owner_user_id = body.get("owner_user_id") if "owner_user_id" in body else None
        if "owner_user_id" in body:
            # The outer gate now admits editors; ownership itself must
            # not. Without this an editor could hand the channel away.
            if actor_role != OWNER:
                return Response(
                    {"error": "Only the channel owner can transfer ownership."},
                    status=status.HTTP_403_FORBIDDEN,
                )
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
            # DjangoValidationError, not ValueError, is what a malformed
            # UUID actually raises — the `ValueError` here never fired.
            # (No ACL change needed: the incoming owner is separately
            # required to be an active ChannelMember just below.)
            except (User.DoesNotExist, DjangoValidationError):
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

    def _patch_pm_title(self, request, channel):
        """Title-only PM patch: rename the backing PROJECT, not the channel.

        The PM channel's title mirrors `ProjectMaster.project_name` (kept
        in sync by `_ensure_pm_channel_for_project`), so the rename is
        applied to the project with the same rules as
        `ProjectMasterView.put`: project-owner-only, non-empty, unique
        within the team. The signal mirrors the new name back onto the
        channel, and the refreshed channel is returned in the standard
        `{channel}` shape so the sockets proxy broadcasts
        `channel.updated` exactly as it does for a GM rename.
        """
        from origin.models.project.prj_models import ProjectMaster  # noqa: PLC0415

        body = request.data or {}
        extra_fields = set(body.keys()) - {"title"}
        if extra_fields or "title" not in body:
            return Response(
                {
                    "error": (
                        "PM channels mirror the project's metadata; only `title` "
                        "can be patched (it renames the project). Edit the "
                        "project for anything else."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        title = (body.get("title") or "").strip()
        if not title:
            return Response(
                {"error": "title must be a non-empty string."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        project = ProjectMaster.objects.filter(project_id=channel.project_id).first()
        if project is None:
            return Response(
                {"error": "Backing project not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        # Same gate as ProjectMasterView.put: renames are project-owner-only.
        if not project.owner_id or str(project.owner_id) != str(request.user.id):
            return Response(
                {"error": "Only the project owner can change name or owner."},
                status=status.HTTP_403_FORBIDDEN,
            )
        collision = (
            ProjectMaster.objects.filter(team=project.team_id, project_name=title)
            .exclude(project_id=project.project_id)
            .exists()
        )
        if collision:
            return Response(
                {"error": "Another project in this team already uses that name."},
                status=status.HTTP_409_CONFLICT,
            )

        project.project_name = title
        # post_save → _ensure_pm_channel_for_project mirrors the name
        # onto Channel.title.
        project.save(update_fields=["project_name", "ts_updated_at"])
        channel.refresh_from_db()
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
        channel = _get_channel_for_roster_admin(channel_id, request.user)
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
        # Scoped to the CHANNEL's team, not the caller's: the caller is
        # already verified as a member of this channel, and the channel
        # is what the new members are being added to. Passing the channel
        # also admits guest-team people holding a grant on it.
        users = _users_in_team(
            unique_ids, channel.team_id, channel if channel.is_external else None
        )
        if users is None:
            return Response(
                {"error": "One or more user_ids not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            added = self._add_members(channel, users, request.user)
        except ExternalGrantError as exc:
            return Response({"error": exc.code}, status=_grant_error_status(exc))
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)

        return Response(
            {"members": ChannelMemberSerializer(added, many=True).data},
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _add_members(channel, users, actor):
        """Write the membership rows, routing external adds through the grant.

        On an external chat the two kinds of member have different rules,
        and running them through one code path is what would lose the
        distinction:

        * A **host-team** person is ordinary channel management, so the
          actor has to belong to the host team. Otherwise a guest-team
          manager could staff the host's own chat.
        * A **guest-team** person goes through
          `add_external_participants`, which is where "only a manager of
          THAT guest team, only their own members, never above the grant's
          ceiling" is enforced. Re-implementing those three checks here is
          exactly how they would drift apart.

        An internal channel keeps its original behaviour untouched: any
        member may add any teammate.
        """
        if not channel.is_external:
            with transaction.atomic():
                return [
                    ChannelMember.objects.update_or_create(
                        channel=channel,
                        user=u,
                        defaults={"is_deleted": False, "role": "member"},
                    )[0]
                    for u in users
                ]

        host_side, external = [], []
        for u in users:
            grant = grant_admitting(ExternalGrant.ObjectType.CHANNEL, channel.id, u.id)
            (external if grant else host_side).append((u, grant))

        if host_side and not is_team_member(channel.team_id, actor.id):
            raise PermissionError("Only a member of the host team can add its people.")

        added = []
        with transaction.atomic():
            for u, _ in host_side:
                obj, _created = ChannelMember.objects.update_or_create(
                    channel=channel,
                    user=u,
                    defaults={"is_deleted": False, "role": "member"},
                )
                added.append(obj)
            for u, grant in external:
                # Writes the ChannelMember row itself, at the clamped role.
                add_external_participants(grant, [u.id], actor)
                added.append(ChannelMember.objects.get(channel=channel, user=u))
        return added


class ChannelJoinView(AuthenticatedAPIView):
    """POST /api/v3/channels/{channel_id}/join/

    Self-service join for a PUBLIC GM. Lets any member of the channel's
    team add *themselves* to an open (non-private) group message — the
    path the chat-search box uses when a user clicks a public GM they
    aren't a member of yet.

    Deliberately separate from `ChannelMembersView.post` (which adds
    *other* users and is membership-gated via `_get_channel_for_user`,
    so it 404s for the very caller here — a non-member). This view scopes
    by *team* membership instead and is constrained to public GMs:

      - kind != GM            -> 400 (DMs/MDMs/PMs aren't open-join).
      - is_private == True    -> 403 (private GMs require the owner-
                                 approval request flow, not self-join).
      - requester not in team -> 404 (don't leak channel existence).

    Idempotent: re-joining (or re-activating a previously-removed
    membership) returns 200 with the existing row rather than erroring.
    """

    def post(self, request, channel_id):
        # Team-scoped lookup (NOT membership-scoped): the whole point is
        # to let a non-member join. 404 hides existence on any miss.
        try:
            channel = Channel.objects.select_related("team", "owner").get(
                id=channel_id, is_deleted=False
            )
        except Channel.DoesNotExist:
            raise Http404("Channel not found.")

        # Requester must belong to the channel's team. Raises Http404 on miss
        # so we don't leak the channel's existence to outsiders.
        _verify_team_member(request.user, channel.team_id)

        if channel.kind != ChannelKind.GM:
            return Response(
                {"error": "Only group messages support self-join."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if channel.is_private:
            return Response(
                {"error": "This group is private; request access from the owner."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Idempotent: a brand-new join creates the row; a user who had
        # previously left (soft-deleted membership) gets it re-activated.
        member, _created = ChannelMember.objects.update_or_create(
            channel=channel,
            user=request.user,
            defaults={"is_deleted": False, "role": "member"},
        )

        return Response(
            {
                "channel": ChannelSerializer(channel, context={"request": request}).data,
                "member": ChannelMemberSerializer(member).data,
            },
            status=status.HTTP_200_OK,
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

    def patch(self, request, channel_id, user_id):
        """Set this member's permission role (editor / viewer).

        Owner or editor may call it. Two invariants mirror the Team and
        Project endpoints: the target must not be the channel owner
        (that's a transfer, handled by `ChannelDetailView.patch`), and
        the new role must be assignable — never owner.

        Roles are written into the EXISTING `ChannelMember.role` column
        via the vocabulary mapping in `services/member_roles.py`
        (editor -> "admin", viewer -> "member"), so no migration and no
        change to the messaging write paths.

        PM channels are rejected: their membership mirrors
        `ProjectMembers`, so the project is the one place a PM role is
        set (`/api/v2/project/member-role/`). Allowing both would create
        two sources of truth for the same person.
        """
        channel = _get_channel_for_user(channel_id, request.user)
        if channel.kind == ChannelKind.DM:
            return Response(
                {"error": "DM channels have no roles."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if channel.kind == ChannelKind.PM:
            return Response(
                {"error": "PM channel roles are managed via the project."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not can_manage(resolve_gm_role(channel, request.user.id)):
            return Response(
                {"error": "Only the channel owner or an editor can change member roles."},
                status=status.HTTP_403_FORBIDDEN,
            )

        member_role = (request.data or {}).get("member_role")
        if not is_assignable(member_role):
            return Response(
                {"error": f"member_role must be one of {list(ASSIGNABLE_ROLES)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if channel.owner_id is not None and str(channel.owner_id) == str(user_id):
            return Response(
                {
                    "error": "The channel owner's role cannot be changed. "
                    "Transfer ownership instead."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            member = ChannelMember.objects.get(channel=channel, user_id=user_id, is_deleted=False)
        except ChannelMember.DoesNotExist:
            raise Http404("Member not found.")

        member.role = member_role_to_channel_role(member_role)
        member.save(update_fields=["role", "ts_updated_at"])

        return Response(ChannelMemberSerializer(member).data, status=status.HTTP_200_OK)

    def delete(self, request, channel_id, user_id):
        channel = _get_channel_for_roster_admin(channel_id, request.user)
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
        # On an external chat, a guest team's owner/editor may also pull
        # one of their OWN people out — that is the other half of running
        # your own roster, and without it a guest team could add someone
        # and then have to ask the host to remove them.
        grant = (
            grant_admitting(ExternalGrant.ObjectType.CHANNEL, channel.id, user_id)
            if channel.is_external
            else None
        )
        if not (is_self or can_manage(resolve_gm_role(channel, request.user.id))):
            if grant is None:
                return Response(
                    {"error": "Only the channel owner or an editor can remove other members."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            try:
                removed = remove_external_participants(grant, [user_id], request.user)
            except ExternalGrantError as exc:
                return Response({"error": exc.code}, status=_grant_error_status(exc))
            if (
                not removed
                and not ChannelMember.objects.filter(channel=channel, user_id=user_id).exists()
            ):
                raise Http404("Member not found.")
            return Response(status=status.HTTP_204_NO_CONTENT)

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
                {"error": ("PM channels mirror the project avatar; edit the project instead.")},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not can_manage(resolve_gm_role(channel, request.user.id)):
            return Response(
                {"error": "Only the channel owner or an editor can change the avatar."},
                status=status.HTTP_403_FORBIDDEN,
            )

        profile_image = request.FILES.get("profile_image")
        if profile_image is None:
            return Response(
                {"error": "profile_image is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Avatars were the one upload family with NO size check at all —
        # not tier-aware, not even a flat cap — so a free user capped at
        # 5 MB per attachment could store an arbitrarily large "avatar".
        # Same tier ceiling as every other upload; see `upload_limits`.
        if res := check_upload_size(request.user, profile_image):
            return res

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


class ChannelSharesView(AuthenticatedAPIView):
    """GET /api/v3/channels/{channel_id}/shares/ — the teams in this chat.

    Exists because the team-scoped `/api/v2/team/share/` cannot answer
    this question for the guest side. A guest participant reads the chat
    from the HOST team's shell, and they are not a member of the host
    team, so a team-scoped lookup keyed on the team they are currently
    viewing refuses them their own share. Keying on the channel instead
    lets both sides use one call.

    Read-only, and deliberately so: offering, revoking and changing a
    ceiling stay on `/api/v2/team/share/` (host managers), and the roster
    stays on this channel's own members endpoints. `canAdmit` tells the
    client which side of the share it is on so it can render the right
    affordances without guessing.
    """

    def get(self, request, channel_id):
        channel = _get_channel_for_roster_admin(channel_id, request.user)
        if not channel.is_external:
            return Response({"shares": []}, status=status.HTTP_200_OK)
        return Response(
            {
                "shares": visible_shares_for_object(
                    ExternalGrant.ObjectType.CHANNEL, channel.id, request.user
                )
            },
            status=status.HTTP_200_OK,
        )


# Cap inline editor uploads. Mirrors `MAX_ATTACHMENT_BYTES` in
# `message_views`; kept as a local constant to avoid a cross-view import.
MAX_INLINE_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB


class ChannelInlineUploadView(AuthenticatedAPIView):
    """POST /api/v3/channels/{channel_id}/uploads/

    Compose-time generic file uploader for the chat editors. BlockNote's
    `uploadFile` callback fires while the user is still composing — before
    any Message exists — so the message-scoped
    `/api/v3/messages/{id}/attachments/` route cannot serve it. This
    stores the file via the default storage backend (local disk in dev,
    S3 in prod — the same backend the FileField attachments use) WITHOUT
    creating a `MessageAttachment` row, and returns an absolute URL that
    the editor embeds as an image/file block in `Message.body` (a
    JSONField, so the URL round-trips verbatim). Restores the pre-v3
    behavior of the deleted `/api/v2/chat/attachment/` endpoint.

    Authorization: 404 (not 403) for non-members, matching the
    existence-hiding rule in `_get_channel_for_user`. Any channel member
    may upload (unlike the message-scoped endpoint, which is sender-only)
    because the file isn't yet tied to a message.

    Orphan files (the user abandons the draft) are possible and accepted —
    the legacy compose-time uploader had the same property.
    """

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, channel_id):
        channel = _get_channel_for_user(channel_id, request.user)

        file = request.FILES.get("file")
        if file is None:
            return Response(
                {"error": "Missing multipart field 'file'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if res := check_upload_size(request.user, file, fallback_bytes=MAX_INLINE_UPLOAD_BYTES):
            return res

        # default_storage sanitises the name and collision-suffixes it; the
        # channel-scoped prefix keeps inline uploads grouped per chat.
        stored_name = default_storage.save(
            f"chats/{channel.id}/inline/{uuid.uuid4()}-{file.name}", file
        )
        url = request.build_absolute_uri(default_storage.url(stored_name))
        # Behind Railway's TLS-terminating proxy `request.scheme` is "http"
        # even though the public origin is "https", so build_absolute_uri
        # stamps an http:// URL. That URL is persisted verbatim into
        # Message.body, and the https SPA then can't fetch it (download) or
        # load it without a Mixed-Content warning (image). Trust the proxy's
        # forwarded scheme to fix it here, scoped to this one call site
        # rather than flipping request.is_secure() app-wide via
        # SECURE_PROXY_SSL_HEADER.
        if request.headers.get("X-Forwarded-Proto") == "https" and url.startswith("http://"):
            url = "https://" + url[len("http://") :]
        return Response({"url": url}, status=status.HTTP_201_CREATED)
