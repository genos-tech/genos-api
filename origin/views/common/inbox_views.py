from django.db.models import Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.serializers.common.inbox_serializers import *
from origin.models.common.team_models import *
from origin.models.project.prj_models import *
from origin.models.chat.gm_models import *


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

        myInboxItems = InboxItems.objects.filter(Q(team_id=team_id, receiver=user_id))

        res = []
        for item in myInboxItems:
            res.append(
                {
                    "itemId": item.item_id,
                    "itemBody": item.item_body,
                    "itemType": item.item_type,
                    "isRead": item.is_read,
                    "requestStatus": item.request_status,
                    "tsSent": item.ts_created_at,
                }
            )

        return Response(res, status=status.HTTP_200_OK)


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

        is_already_requested = InboxItems.objects.filter(
            team=data["team"],
            sender=data["sender"],
            receiver=data["receiver"],
            item_type=data["item_type"],
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

        is_already_requested = InboxItems.objects.filter(
            team=data["team"],
            sender=data["sender"],
            receiver=data["receiver"],
            item_type=data["item_type"],
            item_optionals=data["item_optionals"],
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


class InboxItemForJoinGMRequestView(AuthenticatedAPIView):
    def post(self, request):
        gm_owner_id = GMMaster.objects.filter(
            owner_team=request.data["team_id"],
            gm_id=request.data["item_optionals"]["gm_id"],
        ).values_list("owner_user", flat=True)

        data = {
            "team": request.data["team_id"],
            "sender": request.data["sender_id"],
            "receiver": gm_owner_id[0],  # Send to the gm owner
            "item_body": request.data["item_body"],
            "item_type": request.data["item_type"],  # Must be '3'
            "item_optionals": request.data["item_optionals"],
            "is_read": False,
        }

        is_already_requested = InboxItems.objects.filter(
            team=data["team"],
            sender=data["sender"],
            receiver=data["receiver"],
            item_type=data["item_type"],
            item_optionals=data["item_optionals"],
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
