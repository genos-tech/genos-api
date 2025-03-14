from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from origin.views.common import views_users
from origin.views.common.auth_views import UserViewSet, CustomTokenObtainPairView
from origin.views.chat import views_chat_group, views_messages

user_list = UserViewSet.as_view({"get": "list", "post": "create"})

urlpatterns = [
    path("api/v2/user/signup/", user_list, name="signup"),
    path("api/v2/user/info/", user_list, name="user_info"),
    path("api/v2/user/login/", CustomTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v2/user/login/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/v2/user/listAllUsers/", views_users.list_all_users, name="list_all_users"),
    path("api/v2/chatGroup/create/", views_chat_group.create_chat_group, name="create_chat_group"),
    path(
        "api/v2/chatGroup/myGroups/",
        views_chat_group.list_user_chat_groups,
        name="list_user_chat_groups",
    ),
    path("api/v2/chatGroup/join/", views_chat_group.join_chat_group, name="join_chat_group"),
    path("api/v2/test/", views_messages.protected_view, name="protected_view"),
]
