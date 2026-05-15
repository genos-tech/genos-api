from django.urls import path

from origin.search_engine.agent_views import AgentAskView
from origin.search_engine.views import SearchView

urlpatterns = [
    path("api/v2/search/", SearchView.as_view(), name="search"),
    path("api/v2/agent/ask/", AgentAskView.as_view(), name="agent_ask"),
]
