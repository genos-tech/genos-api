"""A reason for tests that change a status through a tool (MIP-1997).

`update_task`, `manage_milestone` and `update_tasks_bulk` refuse a status change without a reason, so a
test that is about something else (a card, a timestamp, a move) and merely sets a status passes this one.
It is added only when the call sets a `status`, and an explicit `reason` in the call wins.
"""

from __future__ import annotations

from typing import Any

TEST_REASON = "Test."


def with_reason(args: dict[str, Any]) -> dict[str, Any]:
    """`{"reason": ...}` when `args` sets a status and gives no reason, else `{}`. Spread it BEFORE the
    call's own arguments: `{**with_reason(args), **args}`."""
    if args.get("status") is not None and "reason" not in args and args.get("action") != "create":
        return {"reason": TEST_REASON}
    return {}
