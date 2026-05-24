from django.urls import path

from origin.search_engine.agent_views import (
    AgentAskView,
    AgentDecideView,
    AgentSessionDetailView,
    AgentSessionsListView,
    AgentUsageView,
)
from origin.search_engine.views import SearchView

urlpatterns = [
    path("api/v2/search/", SearchView.as_view(), name="search"),
    path("api/v2/agent/ask/", AgentAskView.as_view(), name="agent_ask"),
    path("api/v2/agent/decide/", AgentDecideView.as_view(), name="agent_decide"),
    path("api/v2/agent/usage/", AgentUsageView.as_view(), name="agent_usage"),
    path(
        "api/v2/agent/sessions/",
        AgentSessionsListView.as_view(),
        name="agent_sessions_list",
    ),
    path(
        "api/v2/agent/sessions/<str:session_id>/",
        AgentSessionDetailView.as_view(),
        name="agent_session_detail",
    ),
]
