import os

from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.common.user_models import CustomUser
from origin.serializers.common.user_serializers import UserSerializer


#############################
# User views
#############################
class UserProfileView(AuthenticatedAPIView):
    def put(self, request):
        request_user_id = request.user.id

        user_id = request.data.get("user_id")

        if not user_id:
            return Response(
                {"error": "user_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        else:
            if str(request_user_id) != str(user_id):
                return Response(
                    {"error": "Only owner can update user info."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        user = CustomUser.objects.get(id=user_id)

        update_data = request.data.copy()
        # Remove None values from the update_data
        for key, val in request.data.items():
            if val is None:
                update_data.pop(key)

        serializer = UserSerializer(user, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UserProfileImageView(AuthenticatedAPIView):
    parser_classes = [MultiPartParser]

    def put(self, request):
        request_user_id = request.user.id
        user_profile_image = request.FILES.get("user_profile_image")

        if user_profile_image is None:
            return Response(
                {"error": "user_profile_image is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_data = CustomUser.objects.get(id=request_user_id)

        # Best-effort cleanup of the previous profile image file. Django's
        # default `FileSystemStorage.get_available_name` never overwrites
        # — every re-upload of "profile.jpg" lands as
        # `profile_<random>.jpg`, leaving the old file behind forever. On
        # Railway's media volume that's an unbounded disk-bloat leak per
        # user. Deleting first means the new save lands at the canonical
        # path (no random suffix), which also keeps the URL stable across
        # uploads. Wrapped in a try so a missing-on-disk file doesn't
        # block the legitimate upload from succeeding.
        previous_file = user_data.profile_image_url
        if previous_file and previous_file.name:
            try:
                previous_file.delete(save=False)
            except Exception as err:  # pragma: no cover - best-effort cleanup
                print(f"Failed to delete previous profile image: {err}")

        # Only update the FileField
        new_profile_image_data = {
            "profile_image_url": user_profile_image,
        }

        serializer = UserSerializer(user_data, data=new_profile_image_data, partial=True)
        if serializer.is_valid():
            saved_user = serializer.save()

            # `profile_image_url.name` is already the storage path that
            # Django actually wrote (relative to MEDIA_ROOT and reflecting
            # any `get_available_name` rename). Mirror it onto
            # `profile_image_file_name` directly instead of rebuilding the
            # path from `request_user_id` + last segment — the rebuild was
            # equivalent today but coupled to `user_profile_image_path`'s
            # exact `user_profiles/<uuid>/<file>` shape, so any future
            # tweak to that helper would silently drift the served URL.
            saved_user.profile_image_file_name = saved_user.profile_image_url.name
            saved_user.save(update_fields=["profile_image_file_name"])

            return Response(UserSerializer(saved_user).data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
