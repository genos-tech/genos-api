"""Change ONE block of a note over MCP, and read a note's blocks (MIP-1646).

`update_note` can only replace a note's whole body. `edit_note_block` replaces, inserts after, or deletes
a single block by its id, and `get_note(include_blocks=true)` is how a caller learns the ids: each block
comes back with `block_id`, `type`, its `text`, its place in the tree and a `block_hash` that
`edit_note_block` can be given as `base_block_hash` ("refuse if this block changed since I read it").

The edit is made by genos-collab on the note's LIVE doc (`collab_push.edit_note_block`): the editor
autosaves a note's `body` here only after a pause, so splicing this process's copy and pushing it back
whole could undo words typed seconds ago in other blocks. Only a note nobody has ever opened has no live
doc; then the stored `body` is the whole truth and is edited directly (`_stored`).

MCP-only, like `note_threads`: filed in `MCP_ONLY_REGISTRY`, never declared to the in-app agent (a
model-visible tool needs an eval), and kept outside `origin/search_engine/` so a change here starts no
billed eval run.
"""

from origin.search_engine.agent.tools import register_tool
from origin.views.public.mcp.note_blocks import read  # noqa: F401  (`get_note(include_blocks)`)
from origin.views.public.mcp.note_blocks.edit_note_block import EDIT_NOTE_BLOCK

NOTE_BLOCK_TOOLS = (EDIT_NOTE_BLOCK,)

for _tool in NOTE_BLOCK_TOOLS:
    register_tool(_tool)
