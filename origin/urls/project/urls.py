from django.urls import path

from origin.views.project.prj_views import *

urlpatterns = [
    path("api/v2/project/", ProjectMasterView.as_view(), name="project"),
    path("api/v2/project/exist/", CheckProjectExistsView.as_view(), name="exist_project"),
    path("api/v2/project/join/", JoinProjectView.as_view(), name="join_project"),
    path("api/v2/project/leave/", LeaveProjectView.as_view(), name="leave_project"),
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
    # Team-scoped labels applied to PROJECTS (organize the project list).
    # Distinct from `project/tag/` above, which is per-project tags
    # applied to TASKS. See the ProjectLabel model docstring.
    path("api/v2/project/label/", ProjectLabelsView.as_view(), name="project_label"),
    # Permission role (editor/viewer) of one project member. Not the
    # user's `role` job title — see services/member_roles.py.
    path(
        "api/v2/project/member-role/",
        ProjectMemberRoleView.as_view(),
        name="project_member_role",
    ),
    path(
        "api/v2/project/label/assign/",
        ProjectLabelAssignmentView.as_view(),
        name="project_label_assign",
    ),
    path(
        "api/v2/project/task-template/",
        ProjectTaskTemplateView.as_view(),
        name="project_task_template",
    ),
    path(
        "api/v2/project/task-template/defaults/",
        ProjectTemplateDefaultsView.as_view(),
        name="project_template_defaults",
    ),
    path(
        "api/v2/project/task-field-rules/",
        ProjectTaskFieldRulesView.as_view(),
        name="project_task_field_rules",
    ),
    # Per-project custom task fields (definitions only — values ride on
    # the task rows as TaskMaster.custom_field_values).
    path(
        "api/v2/project/custom-fields/",
        ProjectCustomFieldsView.as_view(),
        name="project_custom_fields",
    ),
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
