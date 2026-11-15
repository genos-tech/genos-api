"""Which connected app wrote a chat message, as the whole channel may read it (MIP-1570, MIP-1568).

Ken wants the official Claude / ChatGPT icon on the chat list and on a message. A message is not
read by one person: genos-sockets relays the serialised `Message` unchanged to the whole channel
room, and the chat list serves the same serializer, so nothing can be reduced per viewer on the way
out. What is STORED in `Message.metadata["via"]` is therefore already what everyone may read:

    {"kind": "oauth", "app": "claude" | "chatgpt"}      an official connected app
    {"kind": "oauth"}                                    any other connected app
    (the key is absent)                                  everything else, and every older message

Never a name, a connection id, a key prefix. The kind and family come from the write's source
(`inbox_via.connected_app_via`, the one place that reduces `get_current_source()` for a stored row).

Who writes it: only server code, and only for a write that came through a connected app: the mirror
of a task comment (`unified_writer`) and the card posted for a task made over MCP (`task_card_post`).
Who cannot: a client. Every path that takes client `metadata` goes through `without_via`
(`_allocate_seq_and_create_message`, the card rewrite, the message edit), and an edit by a person drops
a stamp, since the text is theirs now. `for_wire` is the serializer's second defence: whatever is
stored, only a connected app's kind and family leave the API.

The in-app model never reads this key: `fetch_chat_thread`, `list_my_chats`, the briefing and the
timeline read named keys of `metadata` (`taskCommentId`, `systemNotice`) and nothing else.
"""

from __future__ import annotations

from typing import Any

from origin.services import inbox_via
from origin.views.public.mcp.via_privacy import via_for_viewer

KEY = inbox_via.KEY


def without_via(metadata: Any) -> Any:
    """`metadata` minus any `via`; the same object when it has none, and never edited."""
    return inbox_via.without_via(metadata)


def stamped(metadata: Any) -> dict[str, Any]:
    """`metadata` as the write in progress should leave it: any `via` already in it replaced by
    this write's connected-app stamp, or simply removed when the write did not come through one.

    "Replaced", not "kept": the stamp says who wrote the text that is there now."""
    clean = without_via(metadata)
    meta = dict(clean) if isinstance(clean, dict) else {}
    via = inbox_via.connected_app_via()
    if via is not None:
        meta[KEY] = via
    return meta


def for_wire(metadata: Any) -> Any:
    """`metadata` as the API serves it: a `via` is kept only as a connected app's reduced kind
    and family (through `via_for_viewer`, so `SHOW_OAUTH_APP_TO_EVERYONE` acts on what is already
    stored), and anything else under that key is left out. A message with no `via` is returned
    as it is; the input is never edited."""
    if not isinstance(metadata, dict) or KEY not in metadata:
        return metadata
    shown = via_for_viewer(metadata[KEY], None, None)
    rest = {k: v for k, v in metadata.items() if k != KEY}
    if isinstance(shown, dict) and shown.get("kind") == "oauth":
        return {**rest, KEY: shown}
    return rest
