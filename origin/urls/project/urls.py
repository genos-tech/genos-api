from django.urls import path

from origin.views.project.prj_views import *


urlpatterns = [
    path("api/v2/project/", ProjectMasterView.as_view(), name="project"),
    path("api/v2/project/exist/", CheckProjectExistsView.as_view(), name="exist_project"),
    path("api/v2/project/join/", ProjectMembersView.as_view(), name="join_project"),
    path("api/v2/project/projects/", ProjectsView.as_view(), name="projects"),
    path(
        "api/v2/project/projectMembers/",
        ProjectMembersView.as_view(),
        name="project_members",
    ),
    path("api/v2/project/tag/", ProjectTagsView.as_view(), name="project_tag"),
]
