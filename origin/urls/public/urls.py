"""Public API routes — `/api/public/v1/`.

A separate module and a separate prefix, following the precedent
`chat/v3_urls.py` set: a new surface gets its own file with its own URL
map in the docstring, rather than accreting into the v2 pile.

`public` in the path is doing real work. `/api/v2/` and `/api/v3/` are
internal surfaces that change when the client needs them to; anything
under `/api/public/` is a contract with people who are not in this
repository, and the prefix is the reminder that breaking it is different
in kind from breaking the others.

    GET    /api/public/v1/me/
    GET    /api/public/v1/projects/
    GET    /api/public/v1/tasks/
    POST   /api/public/v1/tasks/
    GET    /api/public/v1/tasks/<int:task_id>/
    PATCH  /api/public/v1/tasks/<int:task_id>/
"""

from django.urls import path

from origin.views.public.v1_views import (
    MeView,
    ProjectListView,
    TaskDetailView,
    TaskListCreateView,
)

urlpatterns = [
    path("api/public/v1/me/", MeView.as_view(), name="public_v1_me"),
    path("api/public/v1/projects/", ProjectListView.as_view(), name="public_v1_projects"),
    path("api/public/v1/tasks/", TaskListCreateView.as_view(), name="public_v1_tasks"),
    path(
        "api/public/v1/tasks/<int:task_id>/",
        TaskDetailView.as_view(),
        name="public_v1_task_detail",
    ),
]
