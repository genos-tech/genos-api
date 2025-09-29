from django.urls import path

from origin.views.project.prj_views import *


urlpatterns = [
    path("api/v2/project/", ProjectMasterView.as_view(), name="project"),
    path("api/v2/project/exist/", CheckProjectExistsView.as_view(), name="exist_project"),
    path("api/v2/project/join/", JoinProjectView.as_view(), name="join_project"),
    path(
        "api/v2/project/join/fromInbox/",
        JoinProjectFromInboxView.as_view(),
        name="join_project_from_inbox",
    ),
    path("api/v2/project/projects/", ProjectsView.as_view(), name="projects"),
    path(
        "api/v2/project/members/",
        ProjectMembersView.as_view(),
        name="project_members",
    ),
    path("api/v2/project/tag/", ProjectTagsView.as_view(), name="project_tag"),
    path(
        "api/v2/project/profile/",
        ProjectMasterView.as_view(),
        name="project_profile",
    ),
    path(
        "api/v2/project/profile/image/",
        ProjectProfileImageView.as_view(),
        name="project_profile_image",
    ),
]
