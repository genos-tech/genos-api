from django.urls import path

from origin.views.project.prj_views import *


urlpatterns = [
    path("api/v2/project/create/", ProjectMasterView.as_view(), name="create_project"),
    path("api/v2/project/exist/", CheckProjectExistsView.as_view(), name="exist_project"),
    path("api/v2/project/join/", ProjectMembersView.as_view(), name="exist_project"),
    path("api/v2/project/getMyProjects/", GetMyProjectsView.as_view(), name="get_my_project"),
    path(
        "api/v2/project/getProjectMembers/",
        GetProjectMembersView.as_view(),
        name="get_project_members",
    ),
]
