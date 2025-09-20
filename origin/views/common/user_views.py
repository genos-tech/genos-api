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
                    {"message": f"Only owner can update user info."},
                    status=status.HTTP_200_OK,
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

        # Only update the FileField
        new_profile_image_data = {
            "profile_image_url": user_profile_image,
        }

        serializer = UserSerializer(user_data, data=new_profile_image_data, partial=True)
        if serializer.is_valid():
            saved_user = serializer.save()

            # At this point, Django has stored the file, possibly renamed
            # Now get the actual stored filename
            stored_file_name = saved_user.profile_image_url.name.split("/")[-1]
            saved_user.profile_image_file_name = (
                f"user_profiles/{request_user_id}/{stored_file_name}"
            )
            saved_user.save(update_fields=["profile_image_file_name"])

            return Response(UserSerializer(saved_user).data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
