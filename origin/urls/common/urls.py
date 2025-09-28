from django.urls import path

from origin.views.common.auth_views import *
from origin.views.common.user_views import *
from origin.views.common.team_views import *
from origin.views.common.inbox_views import *
from origin.views.chat.reaction_views import *
from origin.views.utils.extract_page_title_view import get_page_title

user_list = UserViewSet.as_view({"post": "create"})

urlpatterns = [
    # User
    path("api/v2/user/signup/", user_list, name="signup"),
    path("api/v2/user/signin/", CustomTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v2/user/signin/refresh/", CookieTokenRefreshView.as_view(), name="token_refresh"),
    path("api/v2/user/signout/", LogoutView.as_view(), name="signout"),
    path("api/v2/user/profile/", UserProfileView.as_view(), name="update_status"),
    path(
        "api/v2/user/profile/image/",
        UserProfileImageView.as_view(),
        name="update_user_profile_image",
    ),
    # Team
    path("api/v2/team/create/", TeamMasterView.as_view(), name="join_team"),
    path("api/v2/team/exist/", CheckTeamExistsView.as_view(), name="exist_team"),
    path("api/v2/team/join/", TeamMembersView.as_view(), name="exist_team"),
    path(
        "api/v2/team/join/fromInbox/",
        JoinTeamFromInboxView.as_view(),
        name="join_team_from_inbox",
    ),
    path(
        "api/v2/team/profile/image/",
        TeamProfileImageView.as_view(),
        name="update_team_profile_image",
    ),
    path("api/v2/team/getMyTeams/", GetMyTeamsView.as_view(), name="get_my_team"),
    path("api/v2/team/getTeamMembers/", GetTeamMembersView.as_view(), name="get_team_members"),
    path(
        "api/v2/team/getTeamMemberInfo/",
        GetTeamMemberInfoView.as_view(),
        name="get_team_member_info",
    ),
    # Inbox
    path("api/v2/inbox/", InboxItemView.as_view(), name="inbox_item"),
    path(
        "api/v2/inbox/joinTeamRequest/",
        InboxItemForJoinTeamRequestView.as_view(),
        name="inbox_join_team_request_item",
    ),
    path(
        "api/v2/inbox/joinProjectRequest/",
        InboxItemForJoinProjectRequestView.as_view(),
        name="inbox_join_project_request_item",
    ),
    path(
        "api/v2/inbox/joinGMRequest/",
        InboxItemForJoinGMRequestView.as_view(),
        name="inbox_join_gm_request_item",
    ),
    path("api/v2/getPageTitle/", get_page_title, name="get_page_title"),
]
