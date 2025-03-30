from django.urls import path

from origin.views.common.auth_views import *
from origin.views.common.team_views import *
from origin.views.common.search_views import GetTeamMembersAndGroupsView

user_list = UserViewSet.as_view({"post": "create"})

urlpatterns = [
    path("api/v2/user/signup/", user_list, name="signup"),
    path("api/v2/user/signin/", CustomTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v2/user/signin/refresh/", CookieTokenRefreshView.as_view(), name="token_refresh"),
    path("api/v2/user/signout/", LogoutView.as_view(), name="signout"),
    path("api/v2/team/create/", TeamMasterView.as_view(), name="join_team"),
    path("api/v2/team/exist/", CheckTeamExistsView.as_view(), name="exist_team"),
    path("api/v2/team/join/", TeamMembersView.as_view(), name="exist_team"),
    path("api/v2/team/getMyTeams/", GetMyTeamsView.as_view(), name="get_my_team"),
    path(
        "api/v2/team/getAllTeams/", GetAllTeamsView.as_view(), name="get_all_team"
    ),  # TODO: Must be abolish
    path("api/v2/team/getTeamMembers/", GetTeamMembersView.as_view(), name="get_team_members"),
    path(
        "api/v2/search/teamMembersAndGroups/",
        GetTeamMembersAndGroupsView.as_view(),
        name="search_team_members_and_groups",
    ),
]
