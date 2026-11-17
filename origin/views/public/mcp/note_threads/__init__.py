"""Start, reply to and resolve a note's comment threads, on MCP only (MIP-1100, MIP-1644).

`get_note` returns a note's comment threads (MIP-1064). These tools close the loop: they start a
thread on a passage, answer one, and settle it. A comment is stored in the note's Yjs doc, which only
genos-collab writes, so each tool asks collab's internal `create-thread` / `reply-thread` /
`resolve-thread` endpoint (see `services/collab_push.py`). Collab writes through BlockNote's own comment
store, so an open editor shows the change live and can carry on with the thread.

MCP-only, like the secretary tools (`secretary/__init__.py`): filed in `MCP_ONLY_REGISTRY`, never
declared to the in-app agent, and kept outside `origin/search_engine/` so a change here starts no
billed eval run. One tool per module, as in `search_engine/agent/tools/`.

Starting a thread (`comment_on_note`) is the one that touches the body: it needs the anchor text
found in the live doc and a `comment` mark written on it, both done by collab in one transaction.
"""

from origin.search_engine.agent.tools import register_tool
from origin.views.public.mcp.note_threads.comment_on_note import COMMENT_ON_NOTE
from origin.views.public.mcp.note_threads.reply_to_note_comment import REPLY_TO_NOTE_COMMENT
from origin.views.public.mcp.note_threads.resolve_note_comment import RESOLVE_NOTE_COMMENT

NOTE_THREAD_TOOLS = (COMMENT_ON_NOTE, REPLY_TO_NOTE_COMMENT, RESOLVE_NOTE_COMMENT)

for _tool in NOTE_THREAD_TOOLS:
    register_tool(_tool)
