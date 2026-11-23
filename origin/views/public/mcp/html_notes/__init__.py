"""Save an HTML page as a read-only note over MCP (MIP-2214, plan "HTML notes", phases 1a and 1b).

`save_html_note` stores a page (or replaces the page of an HTML note) and answers with warnings and a
fix for each; `get_note` says a note's `body_format` and returns the page on request; `update_note`
and `edit_note_block` refuse an HTML note's text in words that point at `save_html_note`. The storage
and the scan are `origin/services/html_notes.py`; the page is read back over REST at
`GET /api/v3/notes/<type>/<id>/html` under the note's own ACL.

MCP-only, like `note_blocks`: filed in `MCP_ONLY_REGISTRY`, and published only while the `HTML_NOTES`
setting is on (`McpTool.setting`). Kept outside `origin/search_engine/` so a change here starts no
billed eval run: the placeholder body is how search sees an HTML note, so no chunker changes.
"""

from origin.search_engine.agent.tools import register_tool
from origin.views.public.mcp.html_notes import read  # noqa: F401  (`get_note` fields)
from origin.views.public.mcp.html_notes.save_html_note import SAVE_HTML_NOTE

HTML_NOTE_TOOLS = (SAVE_HTML_NOTE,)

for _tool in HTML_NOTE_TOOLS:
    register_tool(_tool)
