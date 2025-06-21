from django.db.models import Count, Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.serializers.common.team_serializers import TeamMasterSerializer, TeamMembersSerializer
from origin.models.common.user_models import CustomUser

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
                "teamId": serializer.data["team_id"],
                "teamName": serializer.data["team_name"],
                "teamEmail": serializer.data["team_email"],
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
                {"error": "Both team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a Team exists in any order
        exists = TeamMaster.objects.filter(Q(team_id=team_id)).exists()

        return Response({"team_exists": exists}, status=status.HTTP_200_OK)


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


class GetMyTeamsView(AuthenticatedAPIView):
    def get(self, request):
        user_id = request.GET.get("user_id")

        if not user_id:
            return Response(
                {"error": "user_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        team_ids = TeamMembers.objects.filter(Q(attendee=user_id)).values_list("team")

        connected_set = set()
        for (team_id,) in team_ids:
            connected_set.add(team_id)

        return Response({"team_ids": list(connected_set)}, status=status.HTTP_200_OK)


class GetAllTeamsView(AuthenticatedAPIView):
    def get(self, request):
        _teams = TeamMaster.objects.values_list("team_id", "team_name", "team_email")
        teams = []
        for (
            team_id,
            team_name,
            team_email,
        ) in _teams:
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
        user_id = request.GET.get("user_id")

        if not user_id or not team_id:
            return Response(
                {"error": "team_id and user_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        attendees = (
            TeamMembers.objects.filter(team=team_id)
            .select_related("attendee")
            .values("attendee__id", "attendee__username", "attendee__email")
        )

        response_data = []
        for attendee in attendees:
            response_data.append({
                "teamId": team_id,
                "userId": attendee["attendee__id"],
                "userName": attendee["attendee__username"],
                "userEmail": attendee["attendee__email"],
                "avatarImgPath": f"{attendee["attendee__email"]}.png",
                "online": False,
            })

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
            .values("team__team_name", "attendee__id", "attendee__username", "attendee__email", "attendee__profile_image_url")
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
                "teamName": member_info["team__team_name"],
                "userId": member_info["attendee__id"],
                "userName": member_info["attendee__username"],
                "userEmail": member_info["attendee__email"],
                "avatarImgPath": member_info["attendee__profile_image_url"],
                "online": False, # TODO: NO NEED?? Should it be passed via WS?
            }

        return Response(response_data, status=status.HTTP_200_OK)
