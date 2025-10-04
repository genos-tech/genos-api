from django.urls import path

from origin.views.task.task_views import *
from origin.views.task.search_views import *


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
]
