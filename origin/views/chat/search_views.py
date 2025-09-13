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
        user_id = request.GET.get("user_id")
        team_id = request.GET.get("team_id")

        if not user_id:
            return Response(
                {"error": "user_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        search_list = []

        # Get all team members
        team_members = (
            TeamMembers.objects.filter(Q(team_id=team_id, attendee__is_system_user=False))
            .select_related("attendee")
            .values(
                "attendee__id", "attendee__username", "attendee__email", "attendee__is_system_user"
            )
        )

        dm_ids_of_team_members = DMMaster.objects.filter(
            Q(team=team_id, user_1_id=user_id) | Q(team=team_id, user_2_id=user_id)
        ).values_list("dm_id", "user_1_id", "user_2_id")

        team_member_id_to_dm_id = {}
        for data in dm_ids_of_team_members:
            if str(data[1]) == user_id:
                team_member_id_to_dm_id[str(data[2])] = int(data[0])
            else:
                team_member_id_to_dm_id[str(data[1])] = int(data[0])

        for member in list(team_members):
            search_list.append(
                {
                    "type": "People",
                    "id": team_member_id_to_dm_id.get(str(member["attendee__id"]), -1),
                    "name": str(member["attendee__username"]),
                    "email": str(member["attendee__email"]),
                    "dmPartnerUser": {
                        "userName": str(member["attendee__username"]),
                        "userId": str(member["attendee__id"]),
                        "avatarImgPath": "",
                        "tsLastSeen": "",
                        "tsJoined": "",
                        "customStatus": "",
                    },
                }
            )

        # Get all groups
        groups_in_team = GMMaster.objects.filter(owner_team=team_id).values("gm_id", "group_name")
        for member in list(groups_in_team):
            search_list.append(
                {
                    "type": "Group",
                    "id": int(member["gm_id"]),
                    "name": member["group_name"],
                    "email": "",
                    "dmPartnerUser": {
                        "userName": "",
                        "userId": "",
                        "avatarImgPath": "",
                        "tsLastSeen": "",
                        "tsJoined": "",
                        "customStatus": "",
                    },
                }
            )

        print("search_list:", search_list)
        return Response(
            search_list,
            status=status.HTTP_200_OK,
        )
