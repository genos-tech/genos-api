from django.urls import path

from origin.views.task.task_views import *
from origin.views.task.search_views import *
from origin.views.task.sprint_views import (
    ProjectSprintsView,
    SprintConfigView,
    SprintView,
)
from origin.views.task.milestone_views import (
    MilestoneAssigneesView,
    MilestoneView,
    ProjectMilestonesView,
)
from origin.views.task.task_activity_views import TaskActivityListView

urlpatterns = [
    path("api/v2/task/", TaskMasterView.as_view(), name="task_create"),
    path("api/v2/task/meta/", TaskMetaView.as_view(), name="task_meta"),
    path("api/v2/task/getTeamTasks/", GetTeamTasksView.as_view(), name="get_team_tasks"),
    path("api/v2/task/getTeamTasksByTag/", GetTeamTasksByTagView.as_view(), name="get_team_tasks"),
    path("api/v2/task/getProjectTasks/", GetProjectTasksView.as_view(), name="get_project_tasks"),
    path(
        "api/v2/task/getMyAssignedTasks/",
        GetMyAssignedTasksView.as_view(),
        name="get_my_assigned_tasks",
    ),
    path("api/v2/task/attachment/", TaskAttachmentsView.as_view(), name="manage_task_attachment"),
    path(
        "api/v2/task/body/attachment/",
        TaskBodyAttachmentView.as_view(),
        name="manage_task_body_attachment",
    ),
    path("api/v2/task/getTask/", GetTaskView.as_view(), name="get_task"),
    path("api/v2/task/childTasks/", ChildTaskView.as_view(), name="child_tasks"),
    path(
        "api/v2/task/getTaskByThreadId/",
        GetTaskByThreadIdView.as_view(),
        name="get_task_by_thread_id",
    ),
    path("api/v2/task/comment/", TaskCommentsView.as_view(), name="add_task_comment"),
    path("api/v2/task/activity/", TaskActivityListView.as_view(), name="task_activity_list"),
    # Dependencies (blocking / blocked-by)
    path(
        "api/v2/task/dependency/",
        TaskDependencyView.as_view(),
        name="task_dependency_create",
    ),
    path(
        "api/v2/task/dependency/list/",
        TaskDependencyView.as_view(),
        name="task_dependency_list",
    ),
    path(
        "api/v2/task/dependency/<int:dependency_id>/",
        TaskDependencyView.as_view(),
        name="task_dependency_detail",
    ),
    # Search
    path(
        "api/v2/search/teamTasks/",
        GetSearchTeamTasksView.as_view(),
        name="search_team_tasks",
    ),
    # Reaction
    path(
        "api/v2/task/comment/reaction/",
        TaskCommentReactionView.as_view(),
        name="task_comment_reaction",
    ),
    # Mention
    path("api/v2/task/comment/mention/", TaskCommentMentionView.as_view(), name="comment_mention"),
    # Sprint
    path("api/v2/sprint/config/", SprintConfigView.as_view(), name="sprint_config"),
    path("api/v2/sprint/", SprintView.as_view(), name="sprint_create"),
    path("api/v2/sprint/list/", ProjectSprintsView.as_view(), name="project_sprints"),
    path("api/v2/sprint/<int:sprint_id>/", SprintView.as_view(), name="sprint_detail"),
    # Milestone
    path("api/v2/milestone/", MilestoneView.as_view(), name="milestone_create"),
    path("api/v2/milestone/list/", ProjectMilestonesView.as_view(), name="project_milestones"),
    path(
        "api/v2/milestone/<int:milestone_id>/",
        MilestoneView.as_view(),
        name="milestone_detail",
    ),
    path(
        "api/v2/milestone/<int:milestone_id>/assignees/",
        MilestoneAssigneesView.as_view(),
        name="milestone_assignees_add",
    ),
    path(
        "api/v2/milestone/<int:milestone_id>/assignees/<uuid:user_id>/",
        MilestoneAssigneesView.as_view(),
        name="milestone_assignees_remove",
    ),
]
