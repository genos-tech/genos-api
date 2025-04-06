from django.urls import path

from origin.views.task.task_views import *


urlpatterns = [
    path("api/v2/task/create/", TaskMasterView.as_view(), name="create_task"),
    path("api/v2/task/getTeamTasks/", GetTeamTasksView.as_view(), name="get_team_tasks"),
    path("api/v2/task/getProjectTasks/", GetProjectTasksView.as_view(), name="get_project_tasks"),
    path(
        "api/v2/task/getMyAssignedTasks/",
        GetMyAssignedTasksView.as_view(),
        name="get_my_assigned_tasks",
    ),
    path(
        "api/v2/task/addTaskAttachment/", TaskAttachmentsView.as_view(), name="add_task_attachment"
    ),
    path("api/v2/task/getTask/", GetPreviewTasksView.as_view(), name="add_task_attachment"),
    path("api/v2/task/updateTask/", TaskMasterView.as_view(), name="update_task"),
    path("api/v2/task/addComment/", TaskCommentsView.as_view(), name="add_task_comment"),
    path("api/v2/task/getComments/", TaskCommentsByIdView.as_view(), name="get_task_comments"),
    path("api/v2/task/addTag/", TaskTagsView.as_view(), name="add_task_tag"),
    path("api/v2/task/getTags/", TaskTagsByIdView.as_view(), name="get_task_tags"),
]
