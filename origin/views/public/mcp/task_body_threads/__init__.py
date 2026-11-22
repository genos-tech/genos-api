"""Comments left ON A TASK BODY, on MCP only (MIP-1863).

A task body is a Yjs document, `task-body:<id>`, and a comment on a passage of it is a thread in that
document's `threads` map, exactly as on a note. `get_task` reads them (`read.py`, the augment that adds
`body_comment_threads`); these tools answer them: start a thread on a passage, reply to one, resolve it.
Each asks genos-collab's internal endpoint for the same operation a note's tool asks (see
`services/collab_push.py`), with the task's document name.

MCP-only, like the note-thread tools: filed in `MCP_ONLY_REGISTRY`, never declared to the in-app agent,
and kept outside `origin/search_engine/` so a change here starts no billed eval run. One tool per module.
"""

from origin.search_engine.agent.tools import register_tool
from origin.views.public.mcp.task_body_threads import read  # noqa: F401  (the get_task augment)
from origin.views.public.mcp.task_body_threads.comment_on_task_body import COMMENT_ON_TASK_BODY
from origin.views.public.mcp.task_body_threads.reply_to_task_body_comment import (
    REPLY_TO_TASK_BODY_COMMENT,
)
from origin.views.public.mcp.task_body_threads.resolve_task_body_comment import (
    RESOLVE_TASK_BODY_COMMENT,
)

TASK_BODY_THREAD_TOOLS = (
    COMMENT_ON_TASK_BODY,
    REPLY_TO_TASK_BODY_COMMENT,
    RESOLVE_TASK_BODY_COMMENT,
)

for _tool in TASK_BODY_THREAD_TOOLS:
    register_tool(_tool)
