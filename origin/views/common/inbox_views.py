from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import *
from origin.models.project.prj_models import *
from origin.serializers.common.inbox_serializers import *
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.incremental import (
    build_delta_response,
    capture_server_time,
    check_since,
)
from rest_framework import status
from rest_framework.response import Response


#############################
# Team Master views
#############################
class InboxItemView(AuthenticatedAPIView):
    def post(self, request):
        data = {
            "team": request.data["team_id"],
            "sender": request.data["sender_id"],
            "receiver": request.data["receiver_id"],
            "item_body": request.data["item_body"],
            "item_type": request.data["item_type"],  # Must be '0'
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
                serializer.save()
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

        inbox_item = InboxItems.objects.get(team=team_id, item_id=item_id)

        update_data = request.data.copy()
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
        user_id = request.GET.get("user_id")

        if not team_id or not user_id:
            return Response(
                {"error": "team_id and user_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
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

        items = []
        for item in qs:
            items.append(
                {
                    "itemId": item.item_id,
                    "itemBody": item.item_body,
                    "itemType": item.item_type,
                    "isRead": item.is_read,
                    "requestStatus": item.request_status,
                    "isDeleted": item.is_deleted,
                    "tsSent": item.ts_created_at,
                }
            )

        return Response(
            build_delta_response({"items": items}, server_time, force_full_reload=force_full),
            status=status.HTTP_200_OK,
        )


class InboxItemForJoinTeamRequestView(AuthenticatedAPIView):
    def post(self, request):
        team_owner_id = TeamMaster.objects.filter(team_id=request.data["team_id"]).values_list(
            "owner__id", flat=True
        )

        data = {
            "team": request.data["team_id"],
            "sender": request.data["sender_id"],
            "receiver": team_owner_id[0],  # Send to the team owner
            "item_body": request.data["item_body"],
            "item_type": request.data["item_type"],  # Must be '1'
            "item_optionals": request.data["item_optionals"],
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
                serializer.save()
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
        project_owner_id = ProjectMaster.objects.filter(
            team_id=request.data["team_id"],
            project_id=request.data["item_optionals"]["project_id"],
        ).values_list("owner", flat=True)

        data = {
            "team": request.data["team_id"],
            "sender": request.data["sender_id"],
            "receiver": project_owner_id[0],  # Send to the project owner
            "item_body": request.data["item_body"],
            "item_type": request.data["item_type"],  # Must be '2'
            "item_optionals": request.data["item_optionals"],
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
                serializer.save()
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
        inbox_item_id = int(request.data["item_id"])
        try:
            sender_id, optionals = InboxItems.objects.values_list("sender", "item_optionals").get(
                item_id=inbox_item_id
            )
        except InboxItems.DoesNotExist:
            return Response(
                {"error": f"Inbox item {inbox_item_id} not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        optionals = optionals or {}
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

        # Idempotent: re-activate a soft-deleted membership rather than
        # duplicating (mirrors ChannelMembersView.post).
        with transaction.atomic():
            ChannelMember.objects.update_or_create(
                channel=gm_channel,
                user_id=sender_id,
                defaults={"is_deleted": False, "role": "member"},
            )

        return Response(
            {"attendee": str(sender_id), "gmName": gm_name, "gmId": str(gm_id)},
            status=status.HTTP_201_CREATED,
        )


class InboxItemForJoinGMRequestView(AuthenticatedAPIView):
    def post(self, request):
        # Resolve the GM's owner via its v3 channel. The FE sends the v3
        # channel UUID as `gm_id` (v3 GMs have no legacy chat id);
        # `Channel.owner` is the GM owner who should receive the request.
        gm_channel = _gm_channel_by_uuid(request.data["item_optionals"]["gm_id"])
        if gm_channel is None or not gm_channel.owner_id:
            return Response(
                {"error": "GM not found or has no owner."},
                status=status.HTTP_404_NOT_FOUND,
            )
        gm_owner_id = gm_channel.owner_id

        data = {
            "team": request.data["team_id"],
            "sender": request.data["sender_id"],
            "receiver": gm_owner_id,  # Send to the gm owner
            "item_body": request.data["item_body"],
            "item_type": request.data["item_type"],  # Must be '3'
            "item_optionals": request.data["item_optionals"],
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
                serializer.save()
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
