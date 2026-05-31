"""
v3 chat-search endpoint.

`GET /api/v3/search/teamMembersAndGroups/?team_id=<uuid>` — the data
source for the chat search box. Replaces the deleted legacy
`/api/v2/search/teamMembersAndGroups/` (removed with the v2 chat REST),
returning v3-native ids so the frontend can open/create channels
DIRECTLY by UUID instead of round-tripping through legacy integer ids
and a client-side snapshot scan.

  People → non-system team members; each is DM-able by `userId`.
  Groups → the team's GM channels; each is openable by `channelId`.

Identity comes from the auth token (the legacy endpoint trusted a
client-supplied `user_id` query param — an IDOR; here `request.user`
is authoritative).
"""

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMembers
from origin.views.chat.channel_views import _verify_team_member
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from rest_framework import status
from rest_framework.response import Response


class SearchTeamMembersAndGroupsView(AuthenticatedAPIView):
    """GET /api/v3/search/teamMembersAndGroups/?team_id=<uuid>"""

    def get(self, request):
        team_id = request.GET.get("team_id")
        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # 404 (not 403) if the requester isn't a member of this team —
        # don't leak team existence. Raises Http404 on miss.
        _verify_team_member(request.user, team_id)

        results = []

        # People: every non-system team member (the requester included —
        # the FE renders the "(You)" badge and the self-DM flow handles
        # the rest). DM-able directly by `userId`.
        members = TeamMembers.objects.filter(
            team_id=team_id,
            attendee__is_system_user=False,
            is_deleted=False,
        ).select_related("attendee")
        for tm in members:
            u = tm.attendee
            results.append(
                {
                    "type": "People",
                    "userId": str(u.id),
                    "name": u.username,
                    "email": u.email,
                    # `CustomUser.profile_image_url` is a FileField, so the
                    # attribute is a `FieldFile` — emit its storage path
                    # (`.name`) string, not the object (DRF's JSON encoder
                    # would try to `.decode()` the raw file bytes → 500).
                    # Mirrors the Channel branch below, whose
                    # `profile_image_url` is already a CharField path.
                    "profileImageUrl": u.profile_image_url.name or None,
                    "isJoined": True,
                }
            )

        # Groups: the team's GM channels, with the requester's join state
        # so the FE can show the join-request modal for private GMs the
        # user isn't in yet.
        gm_channels = list(
            Channel.objects.filter(
                team_id=team_id,
                kind=ChannelKind.GM,
                is_deleted=False,
            )
        )
        joined_channel_ids = set(
            ChannelMember.objects.filter(
                user=request.user,
                channel__in=gm_channels,
                is_deleted=False,
            ).values_list("channel_id", flat=True)
        )
        for ch in gm_channels:
            results.append(
                {
                    "type": "Group",
                    "channelId": str(ch.id),
                    "name": ch.title,
                    "isPrivate": ch.is_private,
                    "isJoined": ch.id in joined_channel_ids,
                    "profileImageUrl": ch.profile_image_url or None,
                    # Retained for back-compat while other legacy entry
                    # points still resolve by legacy id.
                    "legacyChatId": ch.legacy_chat_id,
                }
            )

        return Response({"results": results}, status=status.HTTP_200_OK)
