"""How the write in progress arrived, as a row that keeps no task history may record it (MIP-1488).

A task's activity row stamps the whole `get_current_source()` (`task_signals._record`). A note
version and a to-do have no activity row to put it on, so each carries its own nullable `via`.
What is kept there is narrower than a task's, on purpose: only a call that came through an API key
or a connected app (the MCP kinds). The web app, the in-app Genos AI (Ken, 2026-11-14: it gets no
chip, it is not MCP), an Undo click, a timer and a command leave `None`, which is also what every
row written before this holds. A kind added later records nothing until it is listed here.

The value is the source as `get_current_source()` reports it, copied: for a key `key_id`,
`key_name`, `key_prefix`; for a connected app `connection_id`, `client_name` and the official
`app` family. Its NAME is the owner's alone: every reader goes through
`origin.views.public.mcp.via_privacy.via_for_viewer`.
"""

from __future__ import annotations

from typing import Any

from origin.middleware.current_user import get_current_source

# The credential kinds `McpOAuthCredential.via()` and `ApiKey.via()` stamp.
RECORDED_KINDS = frozenset({"api_key", "oauth"})


def recorded_via() -> dict[str, Any] | None:
    """The `via` the write in progress should leave on a row, or None (nothing to mark)."""
    source = get_current_source()
    if not isinstance(source, dict) or source.get("kind") not in RECORDED_KINDS:
        return None
    return dict(source)
