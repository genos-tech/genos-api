"""The status values a task or milestone may hold, and the check the REST and v1 writers share (MIP-2391).

`TaskMaster.status` and `MilestoneMaster.status` are free text columns. The agent and MCP tools
already take only these six values; the REST and v1 writers used to store any string, so a typo
came back to every reader (board, filters, rollups) as a status nothing understands.

A write that leaves a row's status as it is always passes, even when the stored value is not one of
the six: the check refuses what a caller *sets*, not what a row already holds, so a save that restates
an old value is never rejected and no data is migrated.
"""

from __future__ import annotations

VALID_STATUS_VALUES = ("Open", "WIP", "Blocked", "Pending", "Closed", "Deleted")


def status_value_error(value, current=None) -> str | None:
    """The 400 message for a status a writer is asked to store, or None when it is fine.

    `current` is what the row holds now (None on create). Setting a row's own stored value again is
    allowed whatever it is.
    """
    if isinstance(value, str) and (value in VALID_STATUS_VALUES or value == current):
        return None
    return "status must be one of: " + ", ".join(VALID_STATUS_VALUES) + "."
