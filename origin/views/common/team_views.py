from collections import defaultdict

from django.core.cache import cache
from django.db.models import F, Q
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.inbox_models import InboxItems
from origin.serializers.common.team_serializers import TeamMasterSerializer, TeamMembersSerializer
from origin.views.utils.incremental import (
    build_delta_response,
    capture_server_time,
    parse_since,
)


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
                    "teamOwnerId": serializer.data["owner"],
                    "teamImgPath": serializer.data.get("profile_image_file_name"),
                }
            }
            return Response(data, status=status.HTTP_201_CREATED)

        error = serializer.errors
        error["hint"] = "Try with different team_name"
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


class TeamProfileImageView(AuthenticatedAPIView):
    parser_classes = [MultiPartParser]

    def put(self, request):
        team_id = request.POST.get("team_id")
        team_profile_image = request.FILES.get("team_profile_image")

        if team_profile_image is None:
            return Response(
                {"error": "team_profile_image is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        team_data = TeamMaster.objects.get(team_id=team_id)

        # Only update the FileField
        new_profile_image_data = {
            "profile_image_file": team_profile_image,
        }

        serializer = TeamMasterSerializer(team_data, data=new_profile_image_data, partial=True)
        if serializer.is_valid():
            saved_team = serializer.save()

            # At this point, Django has stored the file, possibly renamed
            # Now get the actual stored filename
            stored_file_name = saved_team.profile_image_file.name.split("/")[-1]
            saved_team.profile_image_file_name = f"team_profiles/{team_id}/{stored_file_name}"
            saved_team.save(update_fields=["profile_image_file_name"])

            return Response(TeamMasterSerializer(saved_team).data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


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
            print("team_info[0]:", team_info[0])
            res = {
                "exist": True,
                "teamDetails": {
                    "teamId": team_info[0]["team_id"],
                    "teamName": team_info[0]["team_name"],
                    "teamEmail": team_info[0]["team_email"],
                    "teamOwnerId": team_info[0]["owner_id"],
                    "teamImgPath": team_info[0]["profile_image_file_name"],
                },
            }
        else:
            res = {"exist": False, "teamDetails": {}}

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

        # Heartbeat hot path — cache for 60s. Invalidated by
        # cache_invalidation.py signals on TeamMembers / CustomUser writes.
        cache_key = f"team:my_teams:{user_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached, status=status.HTTP_200_OK)

        # Single round trip: fetch the user's teams in one query, then fetch
        # all members for those teams in a second query, then group by team
        # in Python. Replaces a 1+N pattern (1 query for teams, N queries for
        # members) — called on every Flask heartbeat per user, so the savings
        # compound across the running fleet.
        raw_my_teams = list(
            TeamMembers.objects.filter(attendee=user_id, team__is_deleted=False).values_list(
                "team__team_id",
                "team__team_name",
                "team__team_email",
                "team__owner",
                "team__profile_image_file_name",
                "team__ts_created_at",
            )
        )

        team_ids = [row[0] for row in raw_my_teams]
        member_rows = (
            TeamMembers.objects.filter(team_id__in=team_ids, attendee__is_system_user=False)
            .select_related("attendee")
            .order_by("attendee__email")
            .annotate(
                teamId=F("team"),
                teamName=F("team__team_name"),
                userId=F("attendee__id"),
                userName=F("attendee__username"),
                userEmail=F("attendee__email"),
                avatarImgPath=F("attendee__profile_image_file_name"),
                tsLastSeen=F("attendee__last_seen"),
                tsJoined=F("attendee__ts_created_at"),
                customStatus=F("attendee__custom_status"),
                isOfflineForced=F("attendee__is_offline_forced"),
                role=F("attendee__role"),
                baseCountry=F("attendee__base_country"),
                isSystemUser=F("attendee__is_system_user"),
            )
            .values(
                "teamId",
                "teamName",
                "userId",
                "userName",
                "userEmail",
                "avatarImgPath",
                "tsLastSeen",
                "tsJoined",
                "customStatus",
                "isOfflineForced",
                "role",
                "baseCountry",
                "isSystemUser",
            )
        )

        members_by_team = defaultdict(list)
        for member in member_rows:
            members_by_team[member["teamId"]].append(member)

        my_teams = [
            {
                "teamId": team[0],
                "teamName": team[1],
                "teamEmail": team[2],
                "teamOwnerId": team[3],
                "teamImgPath": team[4],
                "teamMembers": members_by_team.get(team[0], []),
                "tsCreatedAt": team[5],
            }
            for team in raw_my_teams
        ]

        cache.set(cache_key, my_teams, timeout=60)
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

        # Snapshot server time BEFORE the query. See utils/incremental.py.
        server_time = capture_server_time()
        since = parse_since(request)

        qs = TeamMembers.objects.filter(Q(team_id=team_id, attendee__is_system_user=False))
        if since is None:
            # Full load: hide soft-deleted memberships and users.
            qs = qs.filter(is_deleted=False, attendee__is_deleted=False)
        else:
            # Incremental: catch both membership-level changes (user
            # joined/left a team) and user-level changes (profile edits,
            # account soft-delete) since the last checkpoint.
            qs = qs.filter(Q(ts_updated_at__gt=since) | Q(attendee__ts_updated_at__gt=since))

        attendees = (
            qs.select_related("attendee")
            .order_by("attendee__email")
            .values(
                "is_deleted",
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
                "attendee__is_deleted",
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
                    # Tombstone flag: client evicts when either the
                    # membership row OR the user account itself is
                    # soft-deleted. Only set on incremental responses.
                    "isDeleted": bool(attendee["is_deleted"] or attendee["attendee__is_deleted"]),
                }
            )

        return Response(
            build_delta_response({"members": response_data}, server_time),
            status=status.HTTP_200_OK,
        )


class GetTeamMemberInfoView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")

        if not user_id or not team_id:
            return Response(
                {"error": "team_id and user_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Per-message hot path — cache for 60s. Invalidated by signals when
        # CustomUser or TeamMembers rows change.
        cache_key = f"team:member_info:{team_id}:{user_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached, status=status.HTTP_200_OK)

        member_info = (
            TeamMembers.objects.filter(Q(team=team_id, attendee=user_id))
            .select_related("attendee")
            .order_by("attendee__email")
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
            "customStatus": member_info.get("attendee__custom_status", ""),
            "isSystemUser": member_info.get("attendee__is_system_user", None),
        }

        cache.set(cache_key, response_data, timeout=60)
        return Response(response_data, status=status.HTTP_200_OK)
