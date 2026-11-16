"""The hash that guards a whole-body note write against a stale read (MIP-1647).

`update_note.content_text` replaces the whole note body, and a note is edited by people while an
agent works: it read the body, a person typed, and the agent's write put the old text back over
theirs. Tasks closed this gap in MIP-824 (`task_body_edit.body_hash`, `base_body_hash`). A note has
the same two halves: `get_note` returns `body_hash` and `update_note` refuses a `base_body_hash`
that no longer matches.

The digest is taken over the STORED BLOCKS, canonical JSON: it answers "is this the body I read".
Block ids are part of it (the editor keeps them stable across saves). The title is not: renaming a
note is not a reason to refuse a body edit.

What it cannot see: text a person has typed into an open editor but not yet autosaved. The editor
saves after a short pause, so for those seconds this copy lags the live document. Catching that
needs the check to run against the live doc in genos-collab (MIP-1646 builds that path).

MCP only (`ctx.surface == "mcp"`): the in-app agent's `fetch_note` and `update_note` declarations
and output stay as they were.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def note_body_hash(body: Any) -> str:
    """16 hex chars identifying the stored body a reader was shown."""
    canonical = json.dumps(body or [], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
