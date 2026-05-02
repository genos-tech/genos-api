from rest_framework import status
from rest_framework.response import Response

from origin.models.common.notification_models import NotificationPreference
from origin.serializers.common.notification_serializers import (
    NotificationPreferenceSerializer,
)
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
