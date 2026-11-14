"""Reply to and resolve a note's comment threads, on MCP only (MIP-1100).

`get_note` returns a note's comment threads (MIP-1064). These two tools close the loop: they
answer a thread, and settle it. A comment is stored in the note's Yjs doc, which only genos-collab
writes, so each tool asks collab's internal `reply-thread` / `resolve-thread` endpoint (see
`services/collab_push.py`). Collab writes through BlockNote's own comment store, so an open editor
shows the change live and can carry on with the thread.

MCP-only, like the secretary tools (`secretary/__init__.py`): filed in `MCP_ONLY_REGISTRY`, never
declared to the in-app agent, and kept outside `origin/search_engine/` so a change here starts no
billed eval run. One tool per module, as in `search_engine/agent/tools/`.

Not here, on purpose: starting a thread on a passage (it needs the anchor text found in the body
and a `comment` mark written on it).
"""

from origin.search_engine.agent.tools import register_tool
from origin.views.public.mcp.note_threads.reply_to_note_comment import REPLY_TO_NOTE_COMMENT
from origin.views.public.mcp.note_threads.resolve_note_comment import RESOLVE_NOTE_COMMENT

NOTE_THREAD_TOOLS = (REPLY_TO_NOTE_COMMENT, RESOLVE_NOTE_COMMENT)

for _tool in NOTE_THREAD_TOOLS:
    register_tool(_tool)
