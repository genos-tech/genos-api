from django.db.models import Count, Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.serializers.common.team_serializers import TeamMasterSerializer, TeamMembersSerializer


#############################
# Team Master views
#############################
class TeamMasterView(AuthenticatedAPIView):
    def post(self, request):
        serializer = TeamMasterSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        error = serializer.errors
        error["hint"] = "Try with different team_name"
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


class CheckTeamExistsView(AuthenticatedAPIView):
    def get(self, request):
        team_name = request.GET.get("team_name", None)

        if not team_name:
            return Response(
                {"error": "Both team_name is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a Team exists in any order
        exists = TeamMaster.objects.filter(Q(team_name=team_name)).exists()

        return Response({"team_exists": exists}, status=status.HTTP_200_OK)


class TeamMembersView(AuthenticatedAPIView):
    def post(self, request):
        data = {"team": request.data["team_name"], "attendee": request.data["attendee_email"]}
        serializer = TeamMembersSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class GetMyTeamsView(AuthenticatedAPIView):
    def get(self, request):
        user_email = request.GET.get("user_email")

        if not user_email:
            return Response(
                {"error": "user_email is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Fetch emails that are connected with the given email
        team_ids = TeamMembers.objects.filter(Q(attendee=user_email)).values_list("team")

        connected_set = set()
        for (team_id,) in team_ids:
            connected_set.add(team_id)

        return Response({"team_ids": list(connected_set)}, status=status.HTTP_200_OK)


class GetTeamMembersView(AuthenticatedAPIView):
    def get(self, request):
        user_email = request.GET.get("user_email")
        team_name = request.GET.get("team_name")

        if not user_email:
            return Response(
                {"error": "user_email is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        _team_name = TeamMembers.objects.filter(Q(attendee=user_email)).values("team")
        if len(_team_name) > 0 and _team_name[0]["team"] != team_name:
            return Response(
                {"error": f"You're not in the team `{team_name}`"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        attendees = (
            TeamMembers.objects.filter(team=team_name)
            .select_related("attendee")
            .values("attendee__email", "attendee__username")
        )

        team_members = list(attendees)

        return Response({"team_members": team_members}, status=status.HTTP_200_OK)
