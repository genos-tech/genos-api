"""The plan's chat-history window on `get_chat_thread` (MIP-1367, phase 3). DARK-SHIPPED.

The app hides messages older than a person's plan allows (`message_retention_days`: Core 365 days,
Pro and up unlimited) and never deletes them. A chat read over MCP did not apply that. This applies
it to the one chat read whose answer is a list of messages, with the app's own days
(`quota.get_message_retention_days`, through `mcp_tier_limits.history_window`), and only when
`MCP_TIER_LIMITS_MODE` and the plan's `mcp_apply_message_retention` say so. Off, or with the live
key `None`, `hide_old_messages` returns the payload it was given, untouched.

Why it is an `augment` and not a change to the tool: `fetch_chat_thread` is the in-app agent's too,
and the in-app agent's answers must not change. `McpTool.augment` runs after the tool, on this
surface only.

The other chat reads are windowed by their arguments instead (`limit_gate`: `list_my_mentions` and
`get_timeline` start no earlier than the window), search already applies the viewer's retention
to chat chunks (`search._build_filter`), and the briefing and an item's line look back at most 31
days (`briefing.MAX_LOOKBACK`), which is inside every plan's window.

PAGING. A main-channel page walks back in time (`next_cursor`, `has_more`). Once a message is
hidden, everything older than it is hidden too, so the cursor is dropped: otherwise a model would
page into the hidden history for ever and get an empty page each time. A thread's `has_more` is
about its NEWER replies (the first 100), which hiding old ones does not change.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from django.utils.dateparse import parse_datetime

from origin.services import mcp_tier_limits as limits

log = logging.getLogger(__name__)


def _sent_at(row: dict[str, Any]) -> datetime | None:
    raw = row.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        return parse_datetime(raw)
    except ValueError:
        return None


def hide_old_messages(
    payload: dict[str, Any], args: dict[str, Any], mcp_args: dict[str, Any], ctx
) -> dict[str, Any]:
    """`McpTool.augment` of `get_chat_thread`: drop the messages older than the plan's window."""
    if limits.mode() == limits.OFF:
        return payload
    try:
        window = limits.history_window(ctx.user_id)
        if window is None:
            return payload
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return payload
        kept = []
        for row in messages:
            sent = _sent_at(row) if isinstance(row, dict) else None
            # A message with no readable time is kept: the window is for what we can date.
            if sent is not None and sent < window.cutoff:
                continue
            kept.append(row)
        hidden = len(messages) - len(kept)
        if hidden == 0:
            return payload
        limits.note_history(window, hidden, "get_chat_thread", ctx.user_id)
        if not window.enforced:
            return payload
        out = {**payload, "messages": kept}
        if payload.get("thread_id") is None:
            # Main channel, oldest first: older ones are hidden as well.
            out["has_more"] = False
            out.pop("next_cursor", None)
        out["__summary__"] = _summary(out, len(kept), window.days)
        return out
    except Exception:  # noqa: BLE001 - a window that fails to apply never fails the read
        log.exception("mcp history window failed")
        return payload


def _summary(payload: dict[str, Any], shown: int, days: int) -> str:
    scope = f"{payload.get('chat_type')}:{payload.get('chat_id')}"
    if payload.get("thread_id") is not None:
        scope = f"thread {scope}:thread:{payload.get('thread_id')}"
    else:
        scope = f"channel {scope}"
    if shown == 0:
        return (
            f"{scope} has no messages in the last {days} days. Older messages are hidden by "
            "your plan's chat history window."
        )
    return (
        f"Loaded {shown} messages from {scope} (your plan's history window is the last {days} days)"
    )
