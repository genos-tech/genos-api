from django.db.models import Count, Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.inbox_models import InboxItems
from origin.serializers.common.team_serializers import TeamMasterSerializer, TeamMembersSerializer


#############################
# Team Master views
#############################
class TeamMasterView(AuthenticatedAPIView):
    def post(self, request):

        data = {
            "team_name": request.data["team_name"],
            "team_email": request.data["team_email"],
            "owner": request.data["owner_id"],
        }

        serializer = TeamMasterSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            data = {
                "teamDetails": {
                    "teamId": serializer.data["team_id"],
                    "teamName": serializer.data["team_name"],
                    "teamEmail": serializer.data["team_email"],
                }
            }
            return Response(data, status=status.HTTP_201_CREATED)

        error = serializer.errors
        error["hint"] = "Try with different team_name"
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


class CheckTeamExistsView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id", None)

        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a Team exists in any order
        team_info = TeamMaster.objects.filter(Q(team_id=team_id)).values()
        if len(team_info) == 1:
            res = {
                "exist": True,
                "teamDetails": {
                    "teamId": team_info[0]["team_id"],
                    "teamName": team_info[0]["team_name"],
                    "teamEmail": team_info[0]["team_email"],
                },
            }
        else:
            res = {"exist": False, "teamDetails": []}

        return Response(res, status=status.HTTP_200_OK)


class TeamMembersView(AuthenticatedAPIView):
    def post(self, request):
        data = {"team": request.data["team_id"], "attendee": request.data["attendee_id"]}
        print(data)

        # Check if a Team exists in any order
        exists = TeamMembers.objects.filter(
            Q(team_id=data["team"], attendee_id=data["attendee"])
        ).exists()

        if exists:
            return Response(data, status=status.HTTP_201_CREATED)
        else:
            serializer = TeamMembersSerializer(data=data)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class JoinTeamFromInboxView(AuthenticatedAPIView):
    def post(self, request):
        team_id = request.data["team_id"]
        team_name = request.data["team_name"]
        inbox_item_id = int(request.data["item_id"])

        attendee_id = str(
            InboxItems.objects.filter(item_id=inbox_item_id).values_list("sender")[0][0]
        )

        # Check if the attendee is not joined yet.
        exists = TeamMembers.objects.filter(Q(team_id=team_id, attendee_id=attendee_id)).exists()

        data = {"team": team_id, "attendee": attendee_id}
        if exists:
            data["teamName"] = team_name
            return Response(data, status=status.HTTP_201_CREATED)
        else:
            serializer = TeamMembersSerializer(data=data)
            if serializer.is_valid():
                serializer.save()
                res = serializer.data
                res["teamName"] = team_name
                return Response(res, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class GetMyTeamsView(AuthenticatedAPIView):
    def get(self, request):
        user_id = request.GET.get("user_id")

        if not user_id:
            return Response(
                {"error": "user_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_my_teams = TeamMembers.objects.filter(
            Q(attendee=user_id, team__is_deleted=False)
        ).values_list("team__team_id", "team__team_name", "team__team_email")

        my_teams = []
        for team in raw_my_teams:
            my_teams.append(
                {
                    "teamId": team[0],
                    "teamName": team[1],
                    "teamEmail": team[2],
                }
            )

        return Response(my_teams, status=status.HTTP_200_OK)


class GetAllTeamsView(AuthenticatedAPIView):
    def get(self, request):
        _teams = TeamMaster.objects.values_list("team_id", "team_name", "team_email", "is_deleted")
        teams = []
        for (
            team_id,
            team_name,
            team_email,
            is_deleted,
        ) in _teams:
            if is_deleted == False:
                teams.append(
                    {
                        "teamId": team_id,
                        "teamName": team_name,
                        "teamEmail": team_email,
                    }
                )
        return Response(teams, status=status.HTTP_200_OK)


class GetTeamMembersView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        user_id = request.GET.get("user_id")

        if not user_id or not team_id:
            return Response(
                {"error": "team_id and user_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        attendees = (
            TeamMembers.objects.filter(Q(team_id=team_id, attendee__is_system_user=False))
            .select_related("attendee")
            .values(
                "attendee__id",
                "attendee__username",
                "attendee__email",
                "attendee__profile_image_file_name",
                "attendee__is_offline_forced",
                "attendee__role",
                "attendee__base_country",
                "attendee__custom_status",
                "attendee__ts_created_at",
                "attendee__is_system_user",
            )
        )

        response_data = []
        for attendee in attendees:
            response_data.append(
                {
                    "teamId": team_id,
                    "teamName": team_name,
                    "userId": attendee["attendee__id"],
                    "userName": attendee["attendee__username"],
                    "userEmail": attendee["attendee__email"],
                    "avatarImgPath": attendee["attendee__profile_image_file_name"],
                    "isOfflineForced": (
                        attendee["attendee__is_offline_forced"]
                        if attendee["attendee__is_offline_forced"]
                        else ""
                    ),
                    "role": (attendee["attendee__role"] if attendee["attendee__role"] else ""),
                    "baseCountry": (
                        attendee["attendee__base_country"]
                        if attendee["attendee__base_country"]
                        else ""
                    ),
                    "customStatus": (
                        attendee["attendee__custom_status"]
                        if attendee["attendee__custom_status"]
                        else ""
                    ),
                    "tsLastSeen": "",
                    "tsJoined": attendee["attendee__ts_created_at"],
                }
            )

        return Response(response_data, status=status.HTTP_200_OK)


class GetTeamMemberInfoView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")

        if not user_id or not team_id:
            return Response(
                {"error": "team_id and user_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        member_info = (
            TeamMembers.objects.filter(Q(team=team_id, attendee=user_id))
            .select_related("attendee")
            .values(
                "team__team_name",
                "attendee__id",
                "attendee__username",
                "attendee__email",
                "attendee__profile_image_file_name",
                "attendee__is_offline_forced",
                "attendee__role",
                "attendee__base_country",
                "attendee__custom_status",
                "attendee__is_system_user",
            )
        )

        if len(member_info) == 0:
            return Response(
                {"error": f"Not found the user (id={user_id})."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        elif len(member_info) > 1:
            return Response(
                {"error": f"Found duplicated users (id={user_id})."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        else:
            member_info = member_info[0]

        response_data = {
            "teamId": team_id,
            "teamName": member_info.get("team__team_name", None),
            "userId": member_info.get("attendee__id", None),
            "userName": member_info.get("attendee__username", None),
            "userEmail": member_info.get("attendee__email", None),
            "avatarImgPath": member_info.get("attendee__profile_image_file_name", None),
            "tsLastSeen": "",
            "tsJoined": "",
            "isOfflineForced": member_info.get("attendee__is_offline_forced", ""),
            "role": member_info.get("attendee__role", ""),
            "baseCountry": member_info.get("attendee__base_country", ""),
            "customStatus": member_info.get("member_info__custom_status", ""),
            "isSystemUser": member_info.get("attendee__is_system_user", None),
        }

        return Response(response_data, status=status.HTTP_200_OK)
