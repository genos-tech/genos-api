from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from . import views_auth, views_chat_group, views_messages, views_users

urlpatterns = [
    path("api/v2/user/register/", views_auth.register_user, name="register"),
    path("api/v2/user/login/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v2/user/login/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/v2/user/listAllUsers/", views_users.list_all_users, name="list_all_users"),
    path("api/v2/chatGroup/create/", views_chat_group.create_chat_group, name="create_chat_group"),
    path("api/v2/chatGroup/myGroups/", views_chat_group.list_user_chat_groups, name="list_user_chat_groups"),
    path("api/v2/chatGroup/join/", views_chat_group.join_chat_group, name="join_chat_group"),
    path("api/v2/test/", views_messages.protected_view, name="protected_view"),
]