from django.db.models import Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.serializers.common.inbox_serializers import *
from origin.models.common.team_models import *


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
            "item_type": request.data["item_type"],
            "is_read": False,
        }

        serializer = InboxItemsSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request):
        team_id = request.data["team_id"]
        item_id = request.data["item_id"]

        if not team_id or not item_id:
            return Response(
                {"error": "team_id and item_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        inbox_item = InboxItems.objects.get(team=team_id, item_id=item_id)

        data = {"is_read": bool(request.data["is_read"])}
        print("inbox put data:", data)

        serializer = InboxItemsSerializer(inbox_item, data=data, partial=True)
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
            "receiver": team_owner_id[0],
            "item_body": request.data["item_body"],
            "item_type": request.data["item_type"],
            "is_read": False,
        }

        is_already_requested = (
            len(
                InboxItems.objects.filter(
                    team=data["team"],
                    sender=data["sender"],
                    receiver=data["receiver"],
                    item_type=data["item_type"],
                ).values()
            )
            > 0
        )

        serializer = InboxItemsSerializer(data=data)
        if serializer.is_valid():
            if is_already_requested == False:
                serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)
