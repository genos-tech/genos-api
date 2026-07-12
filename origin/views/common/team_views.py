import hashlib
import logging
import secrets
import time
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db.models import F, Q
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from origin.models.common.inbox_models import InboxItems
from origin.models.common.invite_models import TeamInvite
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.serializers.common.team_serializers import TeamMasterSerializer, TeamMembersSerializer
from origin.services.email import send_templated_email
from origin.services.team_membership import InviteAcceptError, accept_invite
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.incremental import (
    build_delta_response,
    capture_server_time,
    check_since,
)

logger = logging.getLogger(__name__)


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

    def put(self, request):
        """Update editable team-master fields: `team_name` and `owner_id`.

        Only the current owner is allowed (lets the existing owner
        transfer the team to another member before leaving). The new
        owner must already be an active member of the team — otherwise
        the team could be handed off to outsiders. `team_name` is unique
        team-wide; collisions return 409 with a clear error.

        The per-user `team:my_teams:<user_id>` cache has a 60s TTL and
        will refresh on its own; we don't fan out invalidation here.
        """
        team_id = request.data.get("team_id")
        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            team = TeamMaster.objects.get(team_id=team_id)
        except TeamMaster.DoesNotExist:
            return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

        if not team.owner or str(team.owner.id) != str(request.user.id):
            return Response(
                {"error": "Only the team owner can edit the team."},
                status=status.HTTP_403_FORBIDDEN,
            )

        new_name = request.data.get("team_name")
        new_owner_id = request.data.get("owner_id")
        update_fields = []

        if new_name is not None:
            new_name = (new_name or "").strip()
            if not new_name:
                return Response(
                    {"error": "team_name cannot be empty."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            collision = (
                TeamMaster.objects.filter(team_name=new_name)
                .exclude(team_id=team.team_id)
                .exists()
            )
            if collision:
                return Response(
                    {"error": "Another team already uses that name."},
                    status=status.HTTP_409_CONFLICT,
                )
            team.team_name = new_name
            update_fields.append("team_name")

        if new_owner_id is not None and str(new_owner_id) != str(team.owner.id):
            is_active_member = TeamMembers.objects.filter(
                team_id=team_id, attendee_id=new_owner_id, is_deleted=False
            ).exists()
            if not is_active_member:
                return Response(
                    {"error": "The new owner must already be a member of the team."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            team.owner_id = new_owner_id
            update_fields.append("owner")

        if not update_fields:
            return Response(
                {"error": "No editable fields supplied."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        update_fields.append("ts_updated_at")
        team.save(update_fields=update_fields)
        return Response(
            {
                "teamId": str(team.team_id),
                "teamName": team.team_name,
                "teamOwnerId": str(team.owner.id) if team.owner else None,
            },
            status=status.HTTP_200_OK,
        )


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
            # Append `?v=<ms-timestamp>` so the served URL is unique per
            # upload — mirrors UserProfileImageView / ProjectProfileImageView.
            # Local FileSystemStorage collision-suffixes the filename so the
            # path already changes per upload; but on S3/R2/GCS (Railway /
            # GCP, `AWS_S3_FILE_OVERWRITE=True`) the FE's fixed `profile.jpg`
            # name reuses the same path, so without the query string the
            # browser serves the stale cached team avatar. The query string
            # is ignored by media path-matching.
            cache_buster = int(time.time() * 1000)
            saved_team.profile_image_file_name = (
                f"team_profiles/{team_id}/{stored_file_name}?v={cache_buster}"
            )
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

        # Re-join path: a previously soft-deleted membership row is
        # un-deleted in place. Without this, the UniqueConstraint on
        # (team, attendee) would reject the second join attempt and the
        # left-then-rejoin flow would 4xx silently.
        existing = TeamMembers.objects.filter(
            team_id=data["team"], attendee_id=data["attendee"]
        ).first()
        if existing is not None:
            if existing.is_deleted:
                existing.is_deleted = False
                existing.save(update_fields=["is_deleted", "ts_updated_at"])
            return Response(data, status=status.HTTP_201_CREATED)

        serializer = TeamMembersSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class LeaveTeamView(AuthenticatedAPIView):
    """Soft-delete the requester's membership in a team.

    Owners cannot leave (would orphan the team); the frontend hides the
    Leave button when ownerId matches the user, but we re-check here so
    a hand-crafted request can't bypass that. Soft-delete preserves the
    rejoin path: `TeamMembersView.post` un-deletes the row instead of
    inserting a duplicate (which would violate the unique constraint).
    """

    def post(self, request):
        team_id = request.data.get("team_id")
        attendee_id = request.data.get("attendee_id")
        if not team_id or not attendee_id:
            return Response(
                {"error": "team_id and attendee_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Only the requester can leave themselves. Comparing strings
        # because attendee_id arrives as a JSON string but request.user.id
        # is an int on the auth model.
        if str(request.user.id) != str(attendee_id):
            return Response(
                {"error": "You can only leave a team on your own behalf."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            team = TeamMaster.objects.get(team_id=team_id)
        except TeamMaster.DoesNotExist:
            return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

        if team.owner and str(team.owner.id) == str(attendee_id):
            return Response(
                {"error": "The team owner cannot leave the team."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        member = TeamMembers.objects.filter(
            team_id=team_id, attendee_id=attendee_id, is_deleted=False
        ).first()
        if member is None:
            return Response(
                {"error": "You are not a member of this team."},
                status=status.HTTP_404_NOT_FOUND,
            )

        member.is_deleted = True
        member.save(update_fields=["is_deleted", "ts_updated_at"])
        return Response(
            {"team_id": team_id, "attendee_id": attendee_id},
            status=status.HTTP_200_OK,
        )


class JoinTeamFromInboxView(AuthenticatedAPIView):
    def post(self, request):
        team_id = request.data["team_id"]
        team_name = request.data["team_name"]
        inbox_item_id = int(request.data["item_id"])

        attendee_id = str(
            InboxItems.objects.filter(item_id=inbox_item_id).values_list("sender")[0][0]
        )

        # Re-join path: a previously soft-deleted membership row gets
        # un-deleted in place. Without this, the unique constraint on
        # (team, attendee) would reject the insert and the user would
        # stay out of the team after the owner's approve click.
        data = {"team": team_id, "attendee": attendee_id}
        existing = TeamMembers.objects.filter(team_id=team_id, attendee_id=attendee_id).first()
        if existing is not None:
            if existing.is_deleted:
                existing.is_deleted = False
                existing.save(update_fields=["is_deleted", "ts_updated_at"])
            data["teamName"] = team_name
            return Response(data, status=status.HTTP_201_CREATED)

        serializer = TeamMembersSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            res = serializer.data
            res["teamName"] = team_name
            return Response(res, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


#############################
# Team invite views
#############################
class InviteTeamMembersView(AuthenticatedAPIView):
    """Owner-only: email-invite one or more people to a team.

    Returns a per-email result so the modal can show what happened to
    each address. A send failure for one address doesn't abort the batch.
    """

    def post(self, request):
        team_id = request.data.get("team_id")
        emails = request.data.get("emails") or []
        if not team_id or not isinstance(emails, list):
            return Response(
                {"error": "team_id and a list of emails are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            team = TeamMaster.objects.get(team_id=team_id, is_deleted=False)
        except TeamMaster.DoesNotExist:
            return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

        # Owner-only — mirrors TeamMasterView.put. The codebase has no
        # "admin" role, so the owner is the only one who can grow the team.
        if not team.owner or str(team.owner.id) != str(request.user.id):
            return Response(
                {"error": "Only the team owner can invite members."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Captured once so a later-nulled invited_by FK can't break the
        # email body, and to avoid re-reading per address.
        inviter_name = request.user.username
        expiry_days = max(1, settings.TEAM_INVITE_TOKEN_EXPIRY_MINUTES // 1440)

        results = []
        seen = set()
        for raw in emails:
            email = (raw or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)

            try:
                validate_email(email)
            except DjangoValidationError:
                results.append({"email": email, "status": "invalid_email"})
                continue

            # Already an active member of this team? Skip, send nothing.
            member_user = CustomUser.objects.filter(email__iexact=email, is_deleted=False).first()
            if (
                member_user
                and TeamMembers.objects.filter(
                    team_id=team_id, attendee_id=member_user.id, is_deleted=False
                ).exists()
            ):
                results.append({"email": email, "status": "already_member"})
                continue

            # Re-invite: refresh token + expiry on an existing pending
            # invite rather than inserting a duplicate row.
            invite = TeamInvite.objects.filter(
                team=team, invited_email=email, status="pending"
            ).first()
            resent = invite is not None
            raw_token = secrets.token_urlsafe(32)
            if invite is None:
                invite = TeamInvite(team=team, invited_email=email)
            invite.invited_by = request.user
            invite.token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            invite.expires_at = timezone.now() + timedelta(
                minutes=settings.TEAM_INVITE_TOKEN_EXPIRY_MINUTES
            )
            invite.status = "pending"
            invite.save()

            invite_url = f"{settings.FRONTEND_BASE_URL}/accept-invite?token={raw_token}"
            try:
                send_templated_email(
                    to=email,
                    subject=f"You're invited to join {team.team_name} on Genos",
                    template_base="team_invitation",
                    context={
                        "inviter_name": inviter_name,
                        "team_name": team.team_name,
                        "invite_url": invite_url,
                        "expiry_days": expiry_days,
                    },
                )
            except Exception as exc:
                logger.exception("Invite email send failed for %s: %s", email, exc)
                results.append({"email": email, "status": "failed"})
                continue

            results.append(
                {"email": email, "status": "already_invited_resent" if resent else "sent"}
            )

        return Response({"results": results}, status=status.HTTP_200_OK)


class InvitePreviewView(APIView):
    """Public: describe an invite token so the frontend can route the
    visitor (sign up vs sign in vs accept). Reveals nothing about
    invalid/expired tokens beyond the status."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        token = request.query_params.get("token") or ""
        invalid = {"valid": False, "status": "invalid"}
        if not token:
            return Response(invalid, status=status.HTTP_200_OK)

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        invite = TeamInvite.objects.filter(token_hash=token_hash).first()
        if invite is None or invite.status != "pending":
            return Response(invalid, status=status.HTTP_200_OK)
        if invite.expires_at <= timezone.now():
            return Response({"valid": False, "status": "expired"}, status=status.HTTP_200_OK)
        if invite.team is None or invite.team.is_deleted:
            return Response(invalid, status=status.HTTP_200_OK)

        account_exists = CustomUser.objects.filter(
            email__iexact=invite.invited_email, is_deleted=False
        ).exists()
        return Response(
            {
                "valid": True,
                "status": "account_exists" if account_exists else "no_account",
                "team_name": invite.team.team_name,
                "invited_email": invite.invited_email,
            },
            status=status.HTTP_200_OK,
        )


class InviteAcceptView(AuthenticatedAPIView):
    """Authenticated: consume an invite for the current user. The user's
    email must match the invited address — enforced in accept_invite, so
    a forwarded link can't pull a different account into the team."""

    def post(self, request):
        token = request.data.get("token") or ""
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        invite = TeamInvite.objects.filter(token_hash=token_hash).first()
        if invite is None:
            return Response({"detail": "invalid"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            team = accept_invite(invite, request.user)
        except InviteAcceptError as exc:
            return Response({"detail": exc.code}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {"team_id": str(team.team_id), "team_name": team.team_name},
            status=status.HTTP_200_OK,
        )


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
            TeamMembers.objects.filter(
                attendee=user_id, is_deleted=False, team__is_deleted=False
            ).values_list(
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
        since, force_full = check_since(request)

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
            build_delta_response(
                {"members": response_data}, server_time, force_full_reload=force_full
            ),
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
