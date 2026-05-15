"""Agent tool registry.

The four tools the Phase 3 agent can call. Each tool module exports a
single `Tool` instance; `REGISTRY` aggregates them by name so the
controller can dispatch a Gemini function-call to the right `run(...)`.
"""

from origin.search_engine.agent.tools.base import REGISTRY, Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.fetch_chat_thread import FETCH_CHAT_THREAD
from origin.search_engine.agent.tools.fetch_note import FETCH_NOTE
from origin.search_engine.agent.tools.fetch_task import FETCH_TASK
from origin.search_engine.agent.tools.search_kb import SEARCH_KNOWLEDGE_BASE

# Register each tool at import time so REGISTRY is always populated by
# the time the controller asks for a tool by name.
for _t in (SEARCH_KNOWLEDGE_BASE, FETCH_TASK, FETCH_CHAT_THREAD, FETCH_NOTE):
    REGISTRY[_t.name] = _t


__all__ = [
    "FETCH_CHAT_THREAD",
    "FETCH_NOTE",
    "FETCH_TASK",
    "REGISTRY",
    "SEARCH_KNOWLEDGE_BASE",
    "Tool",
    "ToolContext",
    "ToolError",
]
