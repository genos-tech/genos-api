from django.db.models import Count, Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.common.team_models import TeamMembers
from origin.models.chat.dm_models import DMMaster
from origin.models.chat.gm_models import GMMaster


class GetTeamMembersAndGroupsView(AuthenticatedAPIView):
    def get(self, request):
        """
        Get all users and groups in the specified team
        """
        user_email = request.GET.get("user_email")
        team_name = request.GET.get("team_name")

        if not user_email:
            return Response(
                {"error": "user_email is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not team_name:
            return Response(
                {"error": "team_name is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        _team_name = TeamMembers.objects.filter(Q(attendee=user_email)).values("team")
        if len(_team_name) > 0 and _team_name[0]["team"] != team_name:
            return Response(
                {"error": f"You're not in the team `{team_name}`"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        search_list = []

        # Get all team members
        team_members = (
            TeamMembers.objects.filter(team=team_name)
            .select_related("attendee")
            .values("attendee__email", "attendee__username")
        )

        dm_ids_of_team_members = DMMaster.objects.filter(
            Q(user_1_email=user_email) | Q(user_2_email=user_email)
        ).values_list("dm_id", "user_1_email", "user_2_email")
        team_member_email_to_dm_id = {}
        for data in dm_ids_of_team_members:
            if data[1] == user_email:
                team_member_email_to_dm_id[data[2]] = int(data[0])
            else:
                team_member_email_to_dm_id[data[1]] = int(data[0])

        for member in list(team_members):
            search_list.append(
                {
                    "type": "People",
                    "id": team_member_email_to_dm_id.get(member["attendee__email"], -1),
                    "name": member["attendee__username"],
                    "email": member["attendee__email"],
                }
            )

        # Get all groups
        groups_in_team = GMMaster.objects.filter(owner_team=team_name).values(
            "gm_id", "group_email", "group_name"
        )
        for member in list(groups_in_team):
            search_list.append(
                {
                    "type": "Group",
                    "id": int(member["gm_id"]),
                    "name": member["group_name"],
                    "email": member["group_email"],
                }
            )

        return Response(
            {"searchList": search_list},
            status=status.HTTP_200_OK,
        )
