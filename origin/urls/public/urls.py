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
    POST   /api/public/v1/mcp            (MCP — JSON-RPC, no trailing slash)
    GET    /api/public/v1/openapi.json   (unauthenticated — the spec)
"""

from django.urls import path

from origin.views.public.mcp.views import McpView
from origin.views.public.openapi import OpenApiSchemaView
from origin.views.public.v1_views import (
    MeView,
    ProjectListView,
    TaskDetailView,
    TaskListCreateView,
)

urlpatterns = [
    # First so the spec is reachable even if a route below is
    # misconfigured — a broken API with readable docs is debuggable.
    path(
        "api/public/v1/openapi.json",
        OpenApiSchemaView.as_view(),
        name="public_v1_openapi",
    ),
    path("api/public/v1/me/", MeView.as_view(), name="public_v1_me"),
    path("api/public/v1/projects/", ProjectListView.as_view(), name="public_v1_projects"),
    path("api/public/v1/tasks/", TaskListCreateView.as_view(), name="public_v1_tasks"),
    path(
        "api/public/v1/tasks/<int:task_id>/",
        TaskDetailView.as_view(),
        name="public_v1_task_detail",
    ),
    # No trailing slash: this URL is pasted verbatim into MCP client
    # config, and `openapi.json` above set the precedent that a path
    # people copy by hand doesn't get one. `APPEND_SLASH` can't rescue a
    # POST anyway — the redirect would drop the body.
    path("api/public/v1/mcp", McpView.as_view(), name="public_v1_mcp"),
]
