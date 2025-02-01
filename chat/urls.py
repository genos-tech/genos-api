from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from . import views_auth, views_chat_group, views_messages

urlpatterns = [
    path("api/v1/user/register/", views_auth.register_user, name="register"),
    path("api/v1/user/login/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v1/user/login/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/v1/chatGroup/create/", views_chat_group.create_chat_group, name="create_chat_group"),
    path("api/v1/test/", views_messages.protected_view, name="protected_view"),
]
