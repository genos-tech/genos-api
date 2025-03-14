from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken

from origin.serializers.common.user_serializers import UserSerializer
from origin.models.common.user_models import CustomUser


@api_view(["GET"])
def list_all_users(request):
    all_users = CustomUser.objects.all()
    data = []
    for user in all_users:
        sub_data = {}
        sub_data["id"] = user.id_user
        sub_data["username"] = user.username
        sub_data["dt_last_login"] = user.dt_last_login
        data.append(sub_data)
    return Response(data, status=status.HTTP_200_OK)


# @api_view(["GET"])
# @permission_classes([IsAuthenticated])
# def protected_view():
#     return Response({"message": "You are authenticated!"})
