from django.urls import path

from origin.views.project.archive_views import ProjectArchiveView, ProjectRestoreView
from origin.views.project.automation_views import (
    AutomationRuleDetailView,
    AutomationRuleDryRunView,
    AutomationRuleListView,
    AutomationRuleOrderView,
    AutomationRuleRunsView,
)
from origin.views.project.change_views import ProjectChangesView
from origin.views.project.prj_views import *

urlpatterns = [
    path("api/v2/project/", ProjectMasterView.as_view(), name="project"),
    path("api/v2/project/archive/", ProjectArchiveView.as_view(), name="project_archive"),
    path("api/v2/project/unarchive/", ProjectRestoreView.as_view(), name="project_unarchive"),
    path("api/v2/project/exist/", CheckProjectExistsView.as_view(), name="exist_project"),
    path("api/v2/project/changes/", ProjectChangesView.as_view(), name="project_changes"),
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
        "api/v2/project/member/remove/",
        RemoveProjectMemberView.as_view(),
        name="project_member_remove",
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
    # Named task-filter selections ("Saved Filters" in the filter bar),
    # shared across the project's members.
    path(
        "api/v2/project/saved-task-filters/",
        ProjectSavedTaskFilterView.as_view(),
        name="project_saved_task_filters",
    ),
    # Per-project custom task fields (definitions only — values ride on
    # the task rows as TaskMaster.custom_field_values).
    path(
        "api/v2/project/custom-fields/",
        ProjectCustomFieldsView.as_view(),
        name="project_custom_fields",
    ),
    # A project's own statuses (MIP-2492): the built-in labels and the added ones.
    path(
        "api/v2/projects/<int:project_id>/statuses/",
        ProjectStatusListView.as_view(),
        name="project_statuses",
    ),
    path(
        "api/v2/projects/<int:project_id>/statuses/<int:status_id>/",
        ProjectStatusDetailView.as_view(),
        name="project_status_detail",
    ),
    # A project's automation rules (MIP-2498).
    path(
        "api/v2/projects/<int:project_id>/automation-rules/",
        AutomationRuleListView.as_view(),
        name="automation_rules",
    ),
    path(
        "api/v2/projects/<int:project_id>/automation-rules/order/",
        AutomationRuleOrderView.as_view(),
        name="automation_rule_order",
    ),
    path(
        "api/v2/projects/<int:project_id>/automation-rules/dry-run/",
        AutomationRuleDryRunView.as_view(),
        name="automation_rule_dry_run",
    ),
    path(
        "api/v2/projects/<int:project_id>/automation-rules/<int:rule_id>/",
        AutomationRuleDetailView.as_view(),
        name="automation_rule_detail",
    ),
    path(
        "api/v2/projects/<int:project_id>/automation-rules/<int:rule_id>/runs/",
        AutomationRuleRunsView.as_view(),
        name="automation_rule_runs",
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
