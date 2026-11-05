from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import *
from origin.models.project.prj_models import *
from origin.serializers.common.inbox_serializers import *
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.forged_fields import refuse_forged_fields, stored_values
from origin.views.utils.inbox_approval import (
    JOIN_GM_REQUEST,
    NOTE_ACCESS_REQUEST,
    already_answered,
    is_still_open,
    load_approvable_item,
)
from origin.views.utils.incremental import (
    build_delta_response,
    capture_server_time,
    check_since,
)
from origin.views.utils.request_validators import as_int, body_object, require_fields
from origin.views.utils.scope_guards import is_guest, is_team_member

# Columns of an inbox item that `PUT /inbox/` never changes (MIP-1018).
INBOX_SERVER_OWNED_FIELDS = (
    "team",
    "sender",
    "receiver",
    "item_type",
    "item_body",
    "item_optionals",
    "is_deleted",
)


#############################
# Team Master views
#############################
def _require_team(request, team_id):
    """`None` when the caller belongs to `team_id`, else a 404 `Response`.

    A join REQUEST is the one place a non-member legitimately writes into
    a team, so `InboxItemForJoinTeamRequestView` deliberately does not
    call this — asking to join is the whole point of that endpoint. Every
    other create path here is a member acting inside their own team.
    """
    if not team_id:
        return Response({"error": "team_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    if not is_team_member(team_id, request.user.id) and not is_guest(team_id, request.user.id):
        return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
    return None


# A page of the inbox. Opt-in: a request with no `cursor` and no `limit` returns
# the whole inbox, exactly as before (MIP-806).
_INBOX_PAGE_DEFAULT = 200
_INBOX_PAGE_MAX = 500


def _inbox_cursor(raw):
    """The `item_id` to continue after, or `None` for absent OR unreadable input.

    `item_id` is an auto-increment primary key: unique and totally ordered, so
    unlike the roster and the activity feed there are no ties to break and no
    collation to match. An unreadable cursor is the first page, never a 500 and
    never a page that silently starts somewhere else."""
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _inbox_page_size(request) -> int:
    try:
        wanted = int(request.GET.get("limit", _INBOX_PAGE_DEFAULT))
    except (TypeError, ValueError):
        return _INBOX_PAGE_DEFAULT
    return max(1, min(wanted, _INBOX_PAGE_MAX))


class InboxItemView(AuthenticatedAPIView):
    """Inbox items.

    ⚠️ **The sender is the authenticated user, never a body field.**
    Every create path here used to read `request.data["sender_id"]`
    verbatim, so any authenticated user could post an item claiming to be
    from anyone, to anyone, in any team.

    That is not a disclosure bug — it is a spoofing primitive. Inbox
    items render as "<name> wants to join your project" with an
    Approve button, so a forged sender is a phishing message delivered
    inside the product, wearing a colleague's name, in the surface people
    trust precisely because it is inside the product.

    `receiver` stays a body field: addressing a message to somebody is
    the point. `team` likewise — but see the membership guard below,
    because addressing one into a team you have nothing to do with is
    not.
    """

    def post(self, request):
        body = body_object(request)
        if res := _require_team(request, body.get("team_id")):
            return res
        # Only notices are made here. A request (join team, project, GM, ...)
        # is what an approval acts on, and it has its own endpoint that decides
        # who it goes to: letting the caller pick the type and the receiver
        # here is how a user approved their own "request" into someone else's
        # team (MIP-940).
        if str(body.get("item_type")) != "0":
            return Response(
                {"error": "Only notices (item_type 0) can be created here."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # `receiver_id` and `item_body` were read as `[...]`: a KeyError (500) when missing.
        if res := require_fields(body, "receiver_id", "item_body"):
            return res
        data = {
            "team": body["team_id"],
            "sender": request.user.id,
            "receiver": body["receiver_id"],
            "item_body": body["item_body"],
            "item_type": 0,
            # Optional, unlike the joinXRequest/ endpoints where it's the
            # required routing payload. This endpoint used to drop it on the
            # floor: callers could send `item_optionals` and it silently never
            # persisted, which is why every activity row has null optionals
            # and an activity card can't link to the project/GM it names.
            "item_optionals": body.get("item_optionals"),
            "is_read": False,
        }

        # `.exists()` does an EXISTS subquery (no row materialization), unlike
        # `len(qs.values())` which fetches every matching row just to count.
        already_exist = InboxItems.objects.filter(
            team=data["team"],
            sender=data["sender"],
            receiver=data["receiver"],
            item_body=data["item_body"],
            item_type=data["item_type"],
            is_deleted=False,
        ).exists()

        serializer = InboxItemsSerializer(data=data)
        if serializer.is_valid():
            if already_exist == False:
                item = serializer.save()
                # Web-push the receiver that a new inbox item arrived.
                from origin.services.webpush_dispatch import schedule_push_for_inbox_item

                schedule_push_for_inbox_item(item)
            return Response(
                {
                    "wsType": "inbox",
                    "alreadyExist": already_exist,
                    "data": {
                        "itemId": serializer.data.get("item_id", None),
                        "itemBody": serializer.data.get("item_body", None),
                        "itemType": serializer.data.get("item_type", None),
                        "isRead": serializer.data.get("is_read", None),
                        "tsSent": serializer.data.get("ts_created_at", None),
                        # Echoed so the caller can hand the socket-delivered
                        # payload the same shape a later GET returns. The
                        # sockets handlers copy this dict straight into their
                        # `send()` body, and the client stores that verbatim —
                        # so a field missing here is a field the live item
                        # lacks until the next full refetch.
                        "itemOptionals": serializer.data.get("item_optionals", None),
                    },
                    "receiver": serializer.data.get("receiver", None),
                },
                status=status.HTTP_201_CREATED,
            )

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request):
        team_id = request.data.get("team_id")
        item_id = request.data.get("item_id")

        if team_id is None or item_id is None:
            return Response(
                {"error": "team_id and item_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # An inbox item belongs to its RECEIVER. Without this filter,
        # anyone could mark another person's items read — or flip a
        # pending request to rejected, which silently answers it on their
        # behalf. Scoping the fetch is both the check and the lookup.
        inbox_item = InboxItems.objects.filter(
            team=team_id, item_id=item_id, receiver=request.user
        ).first()
        if inbox_item is None:
            return Response({"error": "Inbox item not found."}, status=status.HTTP_404_NOT_FOUND)

        # The receiver may mark the item read and answer a request (the sockets
        # approve/reject handlers send `is_read` and `request_status`) and
        # nothing else. The whole body used to reach a partial `__all__`
        # serializer, so the receiver could re-address the item to anyone, rewrite
        # its sender, body, type or routing ids, or delete it: a spoofed item
        # planted in another person's inbox, past the sender check on create.
        # A value that is the item's own is accepted and unused; any other is a 400.
        if res := refuse_forged_fields(
            request.data, stored_values(inbox_item, INBOX_SERVER_OWNED_FIELDS)
        ):
            return res
        update_data = {
            key: request.data.get(key)
            for key in ("is_read", "request_status")
            if key in request.data
        }
        # Remove None values from the updated_data if it's None
        if "is_read" in update_data:
            if update_data["is_read"] is not None:
                update_data["is_read"] = bool(update_data.pop("is_read"))

        serializer = InboxItemsSerializer(inbox_item, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def get(self, request):
        team_id = request.GET.get("team_id")
        # The receiver is the CALLER, always. This used to be read from
        # the query string and used verbatim as `receiver=`, so naming
        # someone else's id returned their inbox: join/approve requests,
        # note-access requests and the `itemOptionals` payloads that go
        # with them. Still accepted as a parameter because the client
        # sends it, but only to be checked for agreement.
        user_id = str(request.user.id)

        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        claimed = request.GET.get("user_id")
        if claimed and str(claimed) != user_id:
            return Response(
                {"error": "Only the data owner can request."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Snapshot server time BEFORE the query. See utils/incremental.py.
        server_time = capture_server_time()
        since, force_full = check_since(request)

        qs = InboxItems.objects.filter(Q(team_id=team_id, receiver=user_id))
        if since is None:
            # Full load: hide soft-deleted rows.
            qs = qs.filter(is_deleted=False)
        else:
            # Incremental: include soft-deleted rows so the client can
            # apply tombstones, and bound to anything that has changed
            # since the last checkpoint.
            qs = qs.filter(ts_updated_at__gt=since)

        # Paging is opt-in: with neither `cursor` nor `limit` this is the whole
        # inbox, as it has always been (MIP-806). Ordered by the primary key so
        # a page boundary is exact.
        paged = "cursor" in request.GET or "limit" in request.GET
        after = _inbox_cursor(request.GET.get("cursor"))
        page_size = _inbox_page_size(request)
        if paged:
            qs = qs.order_by("item_id")
            if after is not None:
                qs = qs.filter(item_id__gt=after)
            # One past the page, to know whether another follows without a COUNT.
            qs = qs[: page_size + 1]
        rows = list(qs)
        has_more = paged and len(rows) > page_size
        if has_more:
            rows = rows[:page_size]

        items = []
        for item in rows:
            items.append(
                {
                    "itemId": item.item_id,
                    "itemBody": item.item_body,
                    "itemType": item.item_type,
                    "isRead": item.is_read,
                    "requestStatus": item.request_status,
                    "isDeleted": item.is_deleted,
                    "tsSent": item.ts_created_at,
                    # Request routing payload (e.g. note-access items carry
                    # note_type/note_id/note_title so the client can open the
                    # referenced note). Null for plain activity items.
                    "itemOptionals": item.item_optionals,
                }
            )

        body = build_delta_response({"items": items}, server_time, force_full_reload=force_full)
        if paged:
            # Additive keys; absent on an unpaged request, which is unchanged.
            body["has_more"] = has_more
            body["next_cursor"] = str(rows[-1].item_id) if has_more else None
        return Response(body, status=status.HTTP_200_OK)


class InboxItemForJoinTeamRequestView(AuthenticatedAPIView):
    def post(self, request):
        # A body missing any of these was a KeyError 500, and a `team_id`
        # that is not a UUID a ValidationError 500 (MIP-1014). Now the first
        # is a 400, and a team id that names no team is the "Team not
        # found." below whatever its shape.
        body = body_object(request)
        if any(field not in body for field in ("team_id", "item_body", "item_optionals")):
            return Response(
                {"error": "team_id, item_body and item_optionals are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        team_id = body["team_id"]
        try:
            team_owner_id = list(
                TeamMaster.objects.filter(team_id=team_id, is_deleted=False)
                .exclude(owner__isnull=True)
                .values_list("owner__id", flat=True)
            )
        except (ValueError, ValidationError):
            team_owner_id = []
        if not team_owner_id:
            return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

        data = {
            "team": team_id,
            "sender": request.user.id,
            "receiver": team_owner_id[0],  # Send to the team owner
            "item_body": body["item_body"],
            "item_type": 1,  # Always: the approval refuses anything else
            "item_optionals": body["item_optionals"],
            "is_read": False,
        }

        # Only block on outstanding (pending) requests. After leave →
        # rejoin, the previous approved row is still present with
        # is_deleted=False and request_status="approved"; without the
        # status filter the new request would silently no-op and the
        # owner would never see a fresh inbox notification.
        is_already_requested = InboxItems.objects.filter(
            team=data["team"],
            sender=data["sender"],
            receiver=data["receiver"],
            item_type=data["item_type"],
            request_status="pending",
            is_deleted=False,
        ).exists()

        serializer = InboxItemsSerializer(data=data)
        if serializer.is_valid():
            if is_already_requested == False:
                item = serializer.save()
                # Web-push the request recipient (team / project / GM owner).
                from origin.services.webpush_dispatch import schedule_push_for_inbox_item

                schedule_push_for_inbox_item(item)
            return Response(
                {
                    "wsType": "inbox",
                    "alreadyExist": is_already_requested,
                    "data": {
                        "itemId": serializer.data.get("item_id", None),
                        "itemBody": serializer.data.get("item_body", None),
                        "itemType": serializer.data.get("item_type", None),
                        "isRead": serializer.data.get("is_read", None),
                        "tsSent": serializer.data.get("ts_created_at", None),
                    },
                    "receiver": serializer.data.get("receiver", None),
                },
                status=status.HTTP_201_CREATED,
            )

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


class InboxItemForJoinProjectRequestView(AuthenticatedAPIView):
    def post(self, request):
        body = body_object(request)
        if res := _require_team(request, body.get("team_id")):
            return res
        # The project is named inside `item_optionals`, so that has to be an object with a
        # `project_id` before the lookup (a string or a list there was a TypeError, a missing
        # key a KeyError: both 500). A project id that is not a number names no project.
        if res := require_fields(body, objects=("item_optionals",)):
            return res
        if res := require_fields(
            body["item_optionals"], "project_id", message="item_optionals.project_id is required."
        ):
            return res
        project_id = as_int(body["item_optionals"]["project_id"])
        project_owner_id = []
        if project_id is not None:
            project_owner_id = list(
                ProjectMaster.objects.filter(team_id=body["team_id"], project_id=project_id)
                .exclude(owner__isnull=True)
                .values_list("owner", flat=True)
            )
        if not project_owner_id:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
        if res := require_fields(body, "item_body"):
            return res

        data = {
            "team": body["team_id"],
            "sender": request.user.id,
            "receiver": project_owner_id[0],  # Send to the project owner
            "item_body": body["item_body"],
            "item_type": 2,  # Always: the approval refuses anything else
            "item_optionals": body["item_optionals"],
            "is_read": False,
        }

        # Only block on outstanding (pending) requests. See the team
        # equivalent above for the leave-then-rejoin rationale.
        is_already_requested = InboxItems.objects.filter(
            team=data["team"],
            sender=data["sender"],
            receiver=data["receiver"],
            item_type=data["item_type"],
            item_optionals=data["item_optionals"],
            request_status="pending",
            is_deleted=False,
        ).exists()

        serializer = InboxItemsSerializer(data=data)
        if serializer.is_valid():
            if is_already_requested == False:
                item = serializer.save()
                # Web-push the request recipient (team / project / GM owner).
                from origin.services.webpush_dispatch import schedule_push_for_inbox_item

                schedule_push_for_inbox_item(item)
            return Response(
                {
                    "wsType": "inbox",
                    "alreadyExist": is_already_requested,
                    "data": {
                        "itemId": serializer.data.get("item_id", None),
                        "itemBody": serializer.data.get("item_body", None),
                        "itemType": serializer.data.get("item_type", None),
                        "isRead": serializer.data.get("is_read", None),
                        "tsSent": serializer.data.get("ts_created_at", None),
                    },
                    "receiver": serializer.data.get("receiver", None),
                },
                status=status.HTTP_201_CREATED,
            )

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


def _gm_channel_by_uuid(gm_id):
    """Resolve a v3 GM `Channel` by its UUID.

    The GM-join flow carries the v3 channel UUID as `gm_id`: v3 GMs are
    created natively and have no legacy chat id, so the old
    `resolve_channel(2, <legacy int>)` bridge can never find them.
    Returns the GM Channel, or None on a miss / malformed id / non-GM.
    """
    if not gm_id:
        return None
    try:
        return Channel.objects.filter(id=gm_id, kind=ChannelKind.GM, is_deleted=False).first()
    except (ValueError, ValidationError):
        return None


class JoinGMFromInboxView(AuthenticatedAPIView):
    """POST /api/v2/gm/join/fromInbox/ — approve a GM-join request.

    Replaces the legacy `gm/join/fromInbox/` route deleted in the v3
    cutover. The inbox item (item_type=3) carries the requester in
    `sender` and the target GM's v3 channel UUID + display name in
    `item_optionals.gm_id` / `gm_name`. Resolves the GM by UUID, verifies
    the approver (request.user) is a member of that GM (404 otherwise — no
    existence leak), then idempotently adds the requester as a
    `ChannelMember`. Returns the camelCase shape the Flask
    `approve_join_gm_request` handler expects: `{attendee, gmName, gmId}`.
    """

    def post(self, request):
        # A real GM-join request (MIP-1014): it used to be any inbox item by
        # id, of any type and in any state, and a missing or non-numeric
        # `item_id` was a 500.
        item, error = load_approvable_item(request, item_type=JOIN_GM_REQUEST)
        if error is not None:
            return error
        inbox_item_id, sender_id = item.item_id, item.sender_id

        optionals = item.item_optionals if isinstance(item.item_optionals, dict) else {}
        gm_id = optionals.get("gm_id")
        gm_name = optionals.get("gm_name")

        gm_channel = _gm_channel_by_uuid(gm_id)
        if gm_channel is None:
            return Response(
                {"error": "GM channel not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        # The approver must be the GM owner or an active member (the
        # request is delivered to the GM owner; the owner-id check also
        # covers any GM whose owner lacks an explicit member row). 404 —
        # don't leak existence.
        is_authorized = str(gm_channel.owner_id) == str(request.user.id) or (
            ChannelMember.objects.filter(
                channel=gm_channel, user=request.user, is_deleted=False
            ).exists()
        )
        if not is_authorized:
            return Response(
                {"error": "GM channel not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not is_still_open(item):
            return already_answered()

        # Idempotent: re-activate a soft-deleted membership rather than
        # duplicating (mirrors ChannelMembersView.post).
        with transaction.atomic():
            ChannelMember.objects.update_or_create(
                channel=gm_channel,
                user_id=sender_id,
                defaults={"is_deleted": False, "role": "member"},
            )
            # Settle the consumed request so it stops showing as pending in
            # the approver's inbox (previously never flipped).
            InboxItems.objects.filter(item_id=inbox_item_id).update(request_status="approved")

        # Notify the requester they were approved (push only — there's no
        # inbox item addressed to them).
        from origin.services.webpush_dispatch import schedule_push_to_user

        schedule_push_to_user(
            recipient_id=sender_id,
            category="inbox",
            title=f"Your request to join {gm_name or 'a group chat'} was approved",
            url="/workspace/inbox",
            actor=request.user,
            team_id=gm_channel.team_id,
        )

        return Response(
            {"attendee": str(sender_id), "gmName": gm_name, "gmId": str(gm_id)},
            status=status.HTTP_201_CREATED,
        )


class InboxItemForJoinGMRequestView(AuthenticatedAPIView):
    def post(self, request):
        body = body_object(request)
        if res := _require_team(request, body.get("team_id")):
            return res
        # `item_optionals` has to be an object with a `gm_id` before the lookup (see the
        # project request above for why).
        if res := require_fields(body, objects=("item_optionals",)):
            return res
        if res := require_fields(
            body["item_optionals"], "gm_id", message="item_optionals.gm_id is required."
        ):
            return res
        # Resolve the GM's owner via its v3 channel. The FE sends the v3
        # channel UUID as `gm_id` (v3 GMs have no legacy chat id);
        # `Channel.owner` is the GM owner who should receive the request.
        gm_channel = _gm_channel_by_uuid(body["item_optionals"]["gm_id"])
        if gm_channel is None or not gm_channel.owner_id:
            return Response(
                {"error": "GM not found or has no owner."},
                status=status.HTTP_404_NOT_FOUND,
            )
        gm_owner_id = gm_channel.owner_id
        if res := require_fields(body, "item_body", "item_type"):
            return res

        data = {
            "team": body["team_id"],
            "sender": request.user.id,
            "receiver": gm_owner_id,  # Send to the gm owner
            "item_body": body["item_body"],
            "item_type": body["item_type"],  # Must be '3'
            "item_optionals": body["item_optionals"],
            "is_read": False,
        }

        # Only block on outstanding (pending) requests. See the team
        # equivalent above for the leave-then-rejoin rationale.
        is_already_requested = InboxItems.objects.filter(
            team=data["team"],
            sender=data["sender"],
            receiver=data["receiver"],
            item_type=data["item_type"],
            item_optionals=data["item_optionals"],
            request_status="pending",
            is_deleted=False,
        ).exists()

        serializer = InboxItemsSerializer(data=data)
        if serializer.is_valid():
            if is_already_requested == False:
                item = serializer.save()
                # Web-push the request recipient (team / project / GM owner).
                from origin.services.webpush_dispatch import schedule_push_for_inbox_item

                schedule_push_for_inbox_item(item)
            return Response(
                {
                    "wsType": "inbox",
                    "alreadyExist": is_already_requested,
                    "data": {
                        "itemId": serializer.data.get("item_id", None),
                        "itemBody": serializer.data.get("item_body", None),
                        "itemType": serializer.data.get("item_type", None),
                        "isRead": serializer.data.get("is_read", None),
                        "tsSent": serializer.data.get("ts_created_at", None),
                    },
                    "receiver": serializer.data.get("receiver", None),
                },
                status=status.HTTP_201_CREATED,
            )

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


#############################
# Note access request views
#############################


# Note-type ints match origin.views.utils.note_role (1=personal, 2=task,
# 3=chat). Kept as a local map so the request/grant pair below can
# resolve the right master table without importing the note views.
def _note_master_for_type(note_type):
    from origin.models.note.chat_note_models import ChatNoteMaster
    from origin.models.note.personal_note_models import PersonalNoteMaster
    from origin.models.note.task_note_models import TaskNoteMaster

    return {1: PersonalNoteMaster, 2: TaskNoteMaster, 3: ChatNoteMaster}.get(note_type)


def _note_and_owner(note_type, note_id):
    """Resolve (note, owner_user_id) for one note, or (None, None).

    The owner is the explicit `NotePermissionMaster` ROLE_OWNER row when
    one exists (the canonical "note owner" the role-management UI shows),
    falling back to the master row's `owner` FK for notes created before
    the permission table was backfilled.
    """
    from origin.models.note.common_note_models import NotePermissionMaster
    from origin.views.utils.note_role import ROLE_OWNER

    model = _note_master_for_type(note_type)
    if model is None:
        return None, None
    note = model.objects.filter(note_id=note_id).first()
    if note is None:
        return None, None
    owner_id = (
        NotePermissionMaster.objects.filter(
            note_type=note_type, note_id=note_id, role_id=ROLE_OWNER
        )
        .values_list("user_id", flat=True)
        .first()
    )
    return note, owner_id or getattr(note, "owner_id", None)


class InboxItemForNoteAccessRequestView(AuthenticatedAPIView):
    """POST /api/v2/inbox/noteAccessRequest/ — file a note-access request.

    Body: {team_id, note_type, note_id}. The sender is ALWAYS the
    authenticated caller (never a body field), and the note title is
    resolved server-side — a role-less requester can't read the note, so
    the client doesn't (and must not need to) know its title. Mirrors
    `InboxItemForJoinProjectRequestView`: resolves the note owner as the
    receiver, dedupes pending requests, web-pushes the owner.
    """

    def post(self, request):
        team_id = request.data.get("team_id")
        try:
            note_type = int(request.data.get("note_type"))
            note_id = int(request.data.get("note_id"))
        except (TypeError, ValueError):
            return Response(
                {"error": "note_type and note_id must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not team_id:
            return Response({"error": "team_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        from origin.views.utils.note_role import get_effective_role

        note, owner_id = _note_and_owner(note_type, note_id)
        if note is None or owner_id is None:
            return Response({"error": "Note not found."}, status=status.HTTP_404_NOT_FOUND)
        if str(getattr(note, "team_id", "") or "") != str(team_id):
            return Response({"error": "Note not found."}, status=status.HTTP_404_NOT_FOUND)

        # Someone with any effective role doesn't need to request access.
        if get_effective_role(request.user.id, note_type, note_id, team_id) is not None:
            return Response(
                {"error": "You already have access to this note."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sender = request.user
        note_title = note.title or "Untitled note"
        item_body = [
            {
                "type": "paragraph",
                "props": {
                    "textColor": "default",
                    "textAlignment": "left",
                    "backgroundColor": "default",
                },
                "content": [
                    {
                        "type": "mention",
                        "props": {
                            "teamId": str(team_id),
                            "userId": str(sender.id),
                            "teamName": "N/A",
                            "userName": sender.username or "",
                            "userEmail": sender.email or "",
                            "avatarImgPath": "",
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        },
                    },
                    {"text": " is requesting access to the note - ", "type": "text", "styles": {}},
                    {
                        "text": note_title,
                        "type": "text",
                        "styles": {"bold": True, "textColor": "pink"},
                    },
                    {"text": ".", "type": "text", "styles": {}},
                ],
                "children": [],
            },
            {
                "type": "paragraph",
                "props": {
                    "textColor": "default",
                    "textAlignment": "left",
                    "backgroundColor": "default",
                },
                "content": [
                    {"text": "Waiting for your approval...", "type": "text", "styles": {}},
                ],
                "children": [],
            },
        ]

        data = {
            "team": team_id,
            "sender": sender.id,
            "receiver": owner_id,
            "item_body": item_body,
            "item_type": 4,
            "item_optionals": {
                "note_type": note_type,
                "note_id": note_id,
                "note_title": note_title,
            },
            "is_read": False,
        }

        # Only block on outstanding (pending) requests — a rejected or
        # revoked-then-re-requested flow stays possible (same rationale
        # as the team/project variants above).
        is_already_requested = InboxItems.objects.filter(
            team=data["team"],
            sender=data["sender"],
            receiver=data["receiver"],
            item_type=data["item_type"],
            item_optionals=data["item_optionals"],
            request_status="pending",
            is_deleted=False,
        ).exists()

        serializer = InboxItemsSerializer(data=data)
        if serializer.is_valid():
            if is_already_requested == False:  # noqa: E712 — house style in this module
                item = serializer.save()
                from origin.services.webpush_dispatch import schedule_push_for_inbox_item

                schedule_push_for_inbox_item(item)
            return Response(
                {
                    "wsType": "inbox",
                    "alreadyExist": is_already_requested,
                    "data": {
                        "itemId": serializer.data.get("item_id", None),
                        "itemBody": serializer.data.get("item_body", None),
                        "itemType": serializer.data.get("item_type", None),
                        "isRead": serializer.data.get("is_read", None),
                        "tsSent": serializer.data.get("ts_created_at", None),
                        # Carried so the owner's live-delivered item is
                        # note-clickable without waiting for a reload — mirrors
                        # the field the delta GET now returns.
                        "itemOptionals": serializer.data.get("item_optionals", None),
                    },
                    "receiver": serializer.data.get("receiver", None),
                    "noteTitle": note_title,
                },
                status=status.HTTP_201_CREATED,
            )

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


class GrantNoteAccessFromInboxView(AuthenticatedAPIView):
    """POST /api/v2/note/role/fromInbox/ — approve a note-access request.

    The inbox item (item_type=4) carries the requester in `sender` and
    the target note in `item_optionals`. The approver (request.user)
    must be the note's owner (404 otherwise — no existence leak).
    Grants VIEWER via `NotePermissionMaster` unless the requester
    already has an explicit role (never downgrades), settles the
    request, and web-pushes the requester. Returns the camelCase shape
    the Flask `approve_note_access_request` handler forwards.
    """

    def post(self, request):
        # A real note-access request (MIP-1014): see `JoinGMFromInboxView`.
        item, error = load_approvable_item(request, item_type=NOTE_ACCESS_REQUEST)
        if error is not None:
            return error
        inbox_item_id, sender_id = item.item_id, item.sender_id

        optionals = item.item_optionals if isinstance(item.item_optionals, dict) else {}
        try:
            note_type = int(optionals.get("note_type"))
            note_id = int(optionals.get("note_id"))
        except (TypeError, ValueError):
            return Response(
                {"error": "Inbox item carries no note reference."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        note, owner_id = _note_and_owner(note_type, note_id)
        if note is None or owner_id is None or str(owner_id) != str(request.user.id):
            # Missing note OR non-owner approver — 404 either way so we
            # don't leak whether the note exists.
            return Response({"error": "Note not found."}, status=status.HTTP_404_NOT_FOUND)
        if not is_still_open(item):
            return already_answered()

        from origin.models.note.common_note_models import (
            NoteFolderPermission,
            NotePermissionMaster,
        )
        from origin.views.utils.mention_membership import is_team_folder
        from origin.views.utils.note_role import ROLE_VIEWER, get_explicit_role

        note_title = optionals.get("note_title") or note.title or "Untitled note"
        # For a note in a TEAM folder, the folder is the ACL carrier —
        # approval must land there, as a folder Viewer role. A per-note
        # NotePermissionMaster row (the old behavior) produced an access
        # shape the entire Team Notes UI is blind to: the requester could
        # open the note, but the folder's members dialog didn't list
        # them, the Team Notes sidebar showed them nothing (both read
        # folder grants), and the note opened under the My/Shared bucket.
        team_folder_id = (
            note.folder_id
            if note_type == 1 and is_team_folder(getattr(note, "folder_id", None))
            else None
        )
        with transaction.atomic():
            if team_folder_id is not None:
                # get_or_create, not update_or_create: never downgrade a
                # role the folder already grants (e.g. promoted to
                # Editor between request and approval).
                NoteFolderPermission.objects.get_or_create(
                    folder_id=team_folder_id,
                    user_id=sender_id,
                    defaults={
                        "team_id": note.team_id,
                        "role_id": ROLE_VIEWER,
                        "granted_by_id": request.user.id,
                    },
                )
            # Never downgrade an existing explicit role (e.g. the owner
            # granted Editor between request and approval).
            elif get_explicit_role(sender_id, note_type, note_id) is None:
                NotePermissionMaster.objects.create(
                    team_id=note.team_id,
                    user_id=sender_id,
                    note_type=note_type,
                    note_id=note_id,
                    role_id=ROLE_VIEWER,
                )
            InboxItems.objects.filter(item_id=inbox_item_id).update(request_status="approved")

        from origin.services.webpush_dispatch import schedule_push_to_user

        schedule_push_to_user(
            recipient_id=sender_id,
            category="inbox",
            title=f"Your request to access {note_title} was approved",
            url="/workspace/inbox",
            actor=request.user,
            team_id=note.team_id,
        )

        return Response(
            {
                "attendee": str(sender_id),
                "noteTitle": note_title,
                "noteType": note_type,
                "noteId": note_id,
            },
            status=status.HTTP_201_CREATED,
        )
