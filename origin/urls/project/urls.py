from django.urls import path

from origin.views.project.prj_views import *
from origin.views.project.search_views import *


urlpatterns = [
    path("api/v2/project/", ProjectMasterView.as_view(), name="project_management"),
    path("api/v2/project/exist/", CheckProjectExistsView.as_view(), name="exist_project"),
    path("api/v2/project/join/", ProjectMembersView.as_view(), name="exist_project"),
    path(
        "api/v2/project/getTeamProjects/", GetTeamProjectsView.as_view(), name="get_team_project"
    ),
    path("api/v2/project/getMyProjects/", GetMyProjectsView.as_view(), name="get_my_project"),
    path(
        "api/v2/project/getProjectMembers/",
        GetProjectMembersView.as_view(),
        name="get_project_members",
    ),
    path("api/v2/project/createProjectTag/", ProjectTagsView.as_view(), name="create_project_tag"),
    path("api/v2/project/getProjectTags/", ProjectTagsView.as_view(), name="get_project_tags"),
    # Search
    path(
        "api/v2/search/getTeamTasks/",
        GetTeamTasksView.as_view(),
        name="search_team_tasks",
    ),
]
