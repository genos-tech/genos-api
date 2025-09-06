from django.db.models import Count, Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.common.user_models import CustomUser
from origin.serializers.common.user_serializers import UserSerializer


#############################
# User views
#############################
class UserProfileView(AuthenticatedAPIView):
    def put(self, request):
        request_user_id = request.user.id
        user_id = request.data["user_id"]

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

        data = {
            "custom_status": request.data.get("custom_status", user.custom_status),
            "is_offline_forced": request.data.get("is_offline_forced", user.is_offline_forced),
            "role": request.data.get("role", user.role),
            "base_country": request.data.get("base_country", user.base_country),
        }

        serializer = UserSerializer(user, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
