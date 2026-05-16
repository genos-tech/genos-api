"""Agent tool registry.

The tools the agent can call. Each tool module exports a single `Tool`
instance; `REGISTRY` aggregates them by name so the controller can
dispatch a function-call to the right `run(...)`.

Phase 11 — write-tool surface expansion. Write tools flagged
`requires_approval=True` route through the pause/resume protocol from
Phase 7. Read-only tools execute inline.

Phase 13 — internal tool expansion. Seven new tools covering structured
queries and write operations that were previously impossible or required
fragile semantic search workarounds:
  Read (inline):
    list_projects, list_tasks, get_team_members,
    get_current_user, get_project_summary
  Write (requires_approval):
    assign_task, update_note

Phase 15 — aggregation/analytics surface. Five read-only tools that
return cross-project, time-ranged statistics so the model can answer
PM-style questions ("throughput last week", "top contributors", "which
project has the most notes") without enumerating individual records:
  Read (inline):
    get_task_throughput_stats, get_top_task_closers,
    get_project_activity_ranking, get_workload_distribution,
    get_stale_tasks
"""

from origin.search_engine.agent.tools.add_comment import ADD_COMMENT
from origin.search_engine.agent.tools.assign_task import ASSIGN_TASK
from origin.search_engine.agent.tools.base import REGISTRY, Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.create_note import CREATE_NOTE
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.fetch_chat_thread import FETCH_CHAT_THREAD
from origin.search_engine.agent.tools.fetch_note import FETCH_NOTE
from origin.search_engine.agent.tools.fetch_task import FETCH_TASK
from origin.search_engine.agent.tools.get_current_user import GET_CURRENT_USER
from origin.search_engine.agent.tools.get_project_activity_ranking import (
    GET_PROJECT_ACTIVITY_RANKING,
)
from origin.search_engine.agent.tools.get_project_summary import GET_PROJECT_SUMMARY
from origin.search_engine.agent.tools.get_stale_tasks import GET_STALE_TASKS
from origin.search_engine.agent.tools.get_task_throughput_stats import (
    GET_TASK_THROUGHPUT_STATS,
)
from origin.search_engine.agent.tools.get_team_members import GET_TEAM_MEMBERS
from origin.search_engine.agent.tools.get_top_task_closers import GET_TOP_TASK_CLOSERS
from origin.search_engine.agent.tools.get_workload_distribution import (
    GET_WORKLOAD_DISTRIBUTION,
)
from origin.search_engine.agent.tools.list_projects import LIST_PROJECTS
from origin.search_engine.agent.tools.list_tasks import LIST_TASKS
from origin.search_engine.agent.tools.search_kb import SEARCH_KNOWLEDGE_BASE
from origin.search_engine.agent.tools.update_note import UPDATE_NOTE
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.search_engine.agent.tools.web_search import SEARCH_WEB

# Register at import time so REGISTRY is populated by the time the
# controller asks for a tool by name. Read tools first, then write
# tools — the order only matters for any future iteration of REGISTRY
# (we'd want reads to surface first in tool-list dumps).
for _t in (
    # --- Read tools (Phase 1–11) ---
    SEARCH_KNOWLEDGE_BASE,
    FETCH_TASK,
    FETCH_CHAT_THREAD,
    FETCH_NOTE,
    # --- Read tools (Phase 13) ---
    LIST_PROJECTS,
    LIST_TASKS,
    GET_TEAM_MEMBERS,
    GET_CURRENT_USER,
    GET_PROJECT_SUMMARY,
    # --- Write tools (Phase 11) ---
    CREATE_TASK,
    UPDATE_TASK,
    ADD_COMMENT,
    CREATE_NOTE,
    # --- Write tools (Phase 13) ---
    ASSIGN_TASK,
    UPDATE_NOTE,
    # --- Read tools (Phase 14) ---
    SEARCH_WEB,
    # --- Read tools (Phase 15) — analytics/aggregation ---
    GET_TASK_THROUGHPUT_STATS,
    GET_TOP_TASK_CLOSERS,
    GET_PROJECT_ACTIVITY_RANKING,
    GET_WORKLOAD_DISTRIBUTION,
    GET_STALE_TASKS,
):
    REGISTRY[_t.name] = _t


__all__ = [
    "ADD_COMMENT",
    "ASSIGN_TASK",
    "CREATE_NOTE",
    "CREATE_TASK",
    "FETCH_CHAT_THREAD",
    "FETCH_NOTE",
    "FETCH_TASK",
    "GET_CURRENT_USER",
    "GET_PROJECT_ACTIVITY_RANKING",
    "GET_PROJECT_SUMMARY",
    "GET_STALE_TASKS",
    "GET_TASK_THROUGHPUT_STATS",
    "GET_TEAM_MEMBERS",
    "GET_TOP_TASK_CLOSERS",
    "GET_WORKLOAD_DISTRIBUTION",
    "LIST_PROJECTS",
    "LIST_TASKS",
    "REGISTRY",
    "SEARCH_KNOWLEDGE_BASE",
    "Tool",
    "ToolContext",
    "ToolError",
    "UPDATE_NOTE",
    "UPDATE_TASK",
    "SEARCH_WEB",
]
