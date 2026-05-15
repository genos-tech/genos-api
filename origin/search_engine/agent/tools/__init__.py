"""Agent tool registry.

The tools the agent can call. Each tool module exports a single `Tool`
instance; `REGISTRY` aggregates them by name so the controller can
dispatch a function-call to the right `run(...)`.

Phase 11 — write-tool surface expansion. Four tools currently flagged
`requires_approval=True`: `create_task`, `update_task`, `add_comment`,
`create_note`. All four route through the pause/resume protocol from
Phase 7. Read-only tools (`search_knowledge_base`, `fetch_*`) execute
inline as before.
"""

from origin.search_engine.agent.tools.add_comment import ADD_COMMENT
from origin.search_engine.agent.tools.base import REGISTRY, Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.create_note import CREATE_NOTE
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.fetch_chat_thread import FETCH_CHAT_THREAD
from origin.search_engine.agent.tools.fetch_note import FETCH_NOTE
from origin.search_engine.agent.tools.fetch_task import FETCH_TASK
from origin.search_engine.agent.tools.search_kb import SEARCH_KNOWLEDGE_BASE
from origin.search_engine.agent.tools.update_task import UPDATE_TASK

# Register at import time so REGISTRY is populated by the time the
# controller asks for a tool by name. Read tools first, then write
# tools — the order only matters for any future iteration of REGISTRY
# (we'd want reads to surface first in tool-list dumps).
for _t in (
    SEARCH_KNOWLEDGE_BASE,
    FETCH_TASK,
    FETCH_CHAT_THREAD,
    FETCH_NOTE,
    CREATE_TASK,
    UPDATE_TASK,
    ADD_COMMENT,
    CREATE_NOTE,
):
    REGISTRY[_t.name] = _t


__all__ = [
    "ADD_COMMENT",
    "CREATE_NOTE",
    "CREATE_TASK",
    "FETCH_CHAT_THREAD",
    "FETCH_NOTE",
    "FETCH_TASK",
    "REGISTRY",
    "SEARCH_KNOWLEDGE_BASE",
    "Tool",
    "ToolContext",
    "ToolError",
    "UPDATE_TASK",
]
