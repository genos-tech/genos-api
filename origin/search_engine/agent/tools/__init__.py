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
from origin.search_engine.agent.tools.get_project_summary import GET_PROJECT_SUMMARY
from origin.search_engine.agent.tools.get_team_members import GET_TEAM_MEMBERS
from origin.search_engine.agent.tools.list_projects import LIST_PROJECTS
from origin.search_engine.agent.tools.list_tasks import LIST_TASKS
from origin.search_engine.agent.tools.search_kb import SEARCH_KNOWLEDGE_BASE
from origin.search_engine.agent.tools.update_note import UPDATE_NOTE
from origin.search_engine.agent.tools.update_task import UPDATE_TASK

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
    "GET_PROJECT_SUMMARY",
    "GET_TEAM_MEMBERS",
    "LIST_PROJECTS",
    "LIST_TASKS",
    "REGISTRY",
    "SEARCH_KNOWLEDGE_BASE",
    "Tool",
    "ToolContext",
    "ToolError",
    "UPDATE_NOTE",
    "UPDATE_TASK",
]
