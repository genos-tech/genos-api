from rest_framework import status
from rest_framework.response import Response

from origin.models.common.notification_models import (
    NotificationPreference,
    PushSubscription,
)
from origin.serializers.common.notification_serializers import (
    NotificationPreferenceSerializer,
    PushSubscriptionSerializer,
)
from origin.services import presence
from origin.services.external_grants import presence_team_ids_for_user
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.roster_fields import shown_location


class NotificationPreferenceView(AuthenticatedAPIView):
    """GET / PUT the current user's web-notification preferences.

    The row is created lazily on first GET so callers always see a
    populated default response. Mutations always operate on
    `request.user`'s row, so no client-supplied user_id is required or
    accepted.
    """

    def get(self, request):
        prefs, _ = NotificationPreference.objects.get_or_create(user=request.user)
        serializer = NotificationPreferenceSerializer(prefs)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def put(self, request):
        prefs, _ = NotificationPreference.objects.get_or_create(user=request.user)
        serializer = NotificationPreferenceSerializer(prefs, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PushSubscriptionView(AuthenticatedAPIView):
    """Register / remove the caller's Web Push subscriptions.

    POST upserts by `endpoint` (a browser re-subscribing replaces its keys
    and re-activates the row). DELETE removes one subscription by
    `endpoint` (sent on logout / permission revoke). `user` is always
    `request.user`; the endpoint is globally unique, but we still scope
    DELETE to the caller's rows so one user can't drop another's.
    """

    def post(self, request):
        serializer = PushSubscriptionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data
        PushSubscription.objects.update_or_create(
            endpoint=data["endpoint"],
            defaults={
                "user": request.user,
                "p256dh": data["p256dh"],
                "auth": data["auth"],
                "user_agent": data.get("user_agent", ""),
                # Ties this subscription to the device whose heartbeat
                # decides whether to suppress its pushes.
                "device_id": data.get("device_id", ""),
                "is_active": True,
            },
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    def delete(self, request):
        endpoint = request.data.get("endpoint")
        if not endpoint:
            return Response(
                {"endpoint": "This field is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PresenceHeartbeatView(AuthenticatedAPIView):
    """Mark the caller as having a visible tab (push-suppression presence).

    The frontend POSTs here on a short interval ONLY while the tab is
    visible, sending the same `device_id` it registered its push
    subscription with. The dispatcher then suppresses push for THAT
    device only — the open, focused tab gets the in-app toast, while the
    user's other devices still receive the push.

    The optional `surface` says WHICH conversation is on screen (see
    `services/presence.mark_viewing`), which lets the activity producers
    skip a sidebar row for someone already reading it. Sent on the same
    beat, plus an extra beat whenever the surface changes, so navigating
    between chats retracts the old claim promptly.
    """

    def post(self, request):
        device_id = str(request.data.get("device_id") or "")[:64]
        presence.mark_visible(request.user.id, device_id)
        presence.mark_viewing(request.user.id, request.data.get("surface") or "", device_id)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def delete(self, request):
        """The tab went hidden — stop suppressing push for this device.

        Sent on `visibilitychange`, so push resumes the moment the user
        looks away instead of waiting out the TTL. On iOS that gap was a
        real dead zone: the page's JS is suspended long before the key
        expires, so nothing notified at all in between.
        """
        device_id = str(request.data.get("device_id") or "")[:64]
        presence.clear_visible(request.user.id, device_id)
        presence.clear_viewing(request.user.id, device_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PresenceAudienceView(AuthenticatedAPIView):
    """Which team rooms the socket layer should publish my heartbeat to.

    Asked by `genos-sockets` once per heartbeat (cached there), because
    that service broadcasts online/offline into a room per team and only
    knew about the teams a person can switch into. Anyone reached through
    a cross-team share is in neither side's room, so they rendered
    permanently offline to the people they are actually working with.

    Only ever the caller's own audience. `user_id` is accepted because the
    socket layer's HTTP cache keys on the query string and would otherwise
    serve one person's audience to the next caller — it is checked against
    the token and refused if it names anyone else, which is also what
    stops that cache being poisoned with a chosen key.
    """

    def get(self, request):
        claimed = request.GET.get("user_id")
        if claimed and str(claimed) != str(request.user.id):
            return Response(
                {"error": "Only the data owner can request."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(
            {
                "teamIds": sorted(presence_team_ids_for_user(request.user.id)),
                "profile": presence_profile(request.user),
            },
            status=status.HTTP_200_OK,
        )


def presence_profile(user) -> dict:
    """Who a heartbeat says it is, as the server knows them.

    The socket layer used to rebroadcast whatever `user` object the client
    put on its beat, into every room above. Two things were wrong with that.
    The beat named its own `userId`, `userName` and avatar, so a member could
    overwrite a colleague's row in everyone's roster, or relabel themselves
    as that colleague. And it carried the client's whole `myself`, phone
    number and email included, plus a location the user had opted out of
    sharing, into rooms on the far side of a cross-team share. The socket
    layer now sends this instead, and takes only presence state from the
    beat (see genos-sockets `heartbeat_handlers.py`).

    Narrow enough for the widest audience above. A guest is in their host
    team's room through the team shell `GetMyTeamsView` gives them, so the
    same card reaches colleagues and outsiders alike: no phone number,
    because that is a teammates-only field (`_without_phone`), and no email.
    Neither belongs on a presence ping, and both reach every reader through
    the roster, which is already narrowed per reader.

    Location follows `location_shared` exactly as a roster row read by
    somebody else does. `timezone` is left out: the client pins it from the
    roster row, because a beat never carried it.
    """
    return {
        "userId": str(user.id),
        "userName": user.username,
        "avatarImgPath": user.profile_image_file_name or "",
        "role": user.role or "",
        "baseCountry": user.base_country or "",
        "currentLocation": shown_location(
            user.current_location, user.location_shared, is_self=False
        ),
        "aboutMe": user.about_me or "",
        "tsJoined": user.ts_created_at,
    }
