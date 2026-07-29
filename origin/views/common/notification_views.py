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
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


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
    """

    def post(self, request):
        device_id = str(request.data.get("device_id") or "")[:64]
        presence.mark_visible(request.user.id, device_id)
        return Response(status=status.HTTP_204_NO_CONTENT)
