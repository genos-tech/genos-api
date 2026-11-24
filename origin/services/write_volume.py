"""Rewrite-volume alert for MCP writes (MIP-2265).

An AI app that reads a hostile task, note or message can be told to rewrite many items. The write
tools stay open (no consent gate, decision D7 on MIP-2200: friction for agents versus blast radius),
and the damage is bounded two ways: every overwrite keeps what it replaced (a task description in
`get_task_history` -> `restore_task_body`, a note body in `fetch_note_history` ->
`restore_note_version`), and this module watches the volume. When one API key or connected app has
changed more than `SECURITY_WRITE_ROWS_PER_WINDOW` items in `SECURITY_WRITE_WINDOW_SECONDS` (default
100 in 10 minutes) it writes `alert.write_volume`, which opens an `OpsAlert` that `ops_alert_tick`
mails to the operator, like the export, refused-burst and read-volume alerts. It only alerts: nothing
is paused or refused.

Counting. One successful MCP write call adds the number of items it changed: the length of the
`updates` list for `update_tasks_bulk`, of `tasks` for `create_task_plan`, else 1. A fixed window in
the throttle cache, one counter per credential; the alert fires once per window, when the counter
first reaches the threshold. With the cache down nothing is counted.

Never raises: a detector must not turn a successful write into a 500.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings

from origin.models.common.security_event_models import SecurityAction
from origin.services import throttles

logger = logging.getLogger(__name__)

# Tools whose rows are counted by a list argument; every other write counts one.
_ROW_LIST_ARGUMENT = {"update_tasks_bulk": "updates", "create_task_plan": "tasks"}


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(getattr(settings, name, default)))
    except (TypeError, ValueError):
        return default


def window_seconds() -> int:
    return _int("SECURITY_WRITE_WINDOW_SECONDS", 600)


def threshold() -> int:
    return _int("SECURITY_WRITE_ROWS_PER_WINDOW", 100)


def rows_changed(tool_name: str, args: dict | None) -> int:
    """How many items a successful call of `tool_name` with `args` changed (at least 1)."""
    key = _ROW_LIST_ARGUMENT.get(tool_name)
    if key and isinstance(args, dict) and isinstance(args.get(key), list):
        return max(1, len(args[key]))
    return 1


def note_write(request, credential, *, team_id, tool_name: str, args: dict | None) -> None:
    """Count one successful write by `credential` and raise the alert when it crosses the line."""
    try:
        if credential is None:
            return
        rows = rows_changed(tool_name, args)
        window = window_seconds()
        bucket = int(time.time() // window)
        cache = throttles.throttle_cache()
        key = f"secwrite:{credential.id}:{bucket}"
        if cache.add(key, rows, window * 2):
            total = rows
        else:
            total = cache.incr(key, rows)
        limit = threshold()
        if total < limit or total - rows >= limit:
            return  # below the line, or the alert for this window has already fired
        _alert(request, credential, team_id=team_id, tool_name=tool_name, rows=total, window=window)
    except Exception:
        logger.warning("write volume count failed", exc_info=True)


def _alert(request, credential, *, team_id, tool_name: str, rows: int, window: int) -> None:
    from origin.services.security_events import record_security_event  # noqa: PLC0415

    oauth = getattr(credential, "kind", "api_key") != "api_key"
    logger.warning(
        "security.alert action=%s credential=%s rows=%s window_seconds=%s",
        SecurityAction.ALERT_WRITE_VOLUME,
        credential.id,
        rows,
        window,
    )
    record_security_event(
        SecurityAction.ALERT_WRITE_VOLUME,
        request=getattr(request, "_request", request),
        actor=credential.user,
        api_key=credential,
        team_id=team_id,
        target_type="mcp_connection" if oauth else "api_key",
        target_id=credential.id,
        target_label=credential.label,
        metadata={
            "rows": rows,
            "limit": threshold(),
            "window_seconds": window,
            "last_tool": tool_name,
            "credential": "oauth" if oauth else "api_key",
            "undo": (
                "Task descriptions: get_task_history then restore_task_body. "
                "Notes: fetch_note_history then restore_note_version."
            ),
        },
    )
