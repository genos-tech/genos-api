"""One rule for folding a burst of description edits (MIP-1529).

The body editor auto-saves every few seconds, so one edit session leaves dozens of
`description_edited` audit rows. Two surfaces tell them: the task's Activity feed
(`views/task/task_activity_views.py`) and `get_item_line` (`services/item_lines.py`). Both
fold a run into ONE row with a count, by this rule, so they agree on what a run is:

  * the rows are newest first and the run is anchored on the NEWEST one;
  * a run is consecutive description edits by the same actor, written the same way
    (`via_identity`), each no more than `DESCRIPTION_EDIT_GROUP_WINDOW` after the one before;
  * anything else between two edits (a status change, a comment) closes the run;
  * an edit that kept the previous body (`previousBodyKept`: an agent or API-key rewrite,
    MIP-738) is never folded into a run and never has one folded into it, because it is the one
    place a restore is offered (see `task_activity_views._collapse_description_edits`).

The functions here take the facts through small accessors, so the feed passes its
`TaskActivity` rows and the line passes its events without either having to look like the other.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeVar

# A run of description edits whose adjacent timestamps fall within this window is one row.
DESCRIPTION_EDIT_GROUP_WINDOW = timedelta(minutes=15)

T = TypeVar("T")


def via_identity(metadata: Any) -> tuple[Any, Any, Any]:
    """How a write arrived, as far as the feed's chip tells it apart: the kind, and which key or
    connection for the kinds that name one (MIP-1487).

    `undo_of` and the like are left out on purpose: an Undo click and an autosave are both `app`
    and show the same (no) chip, so they may still fold. Rows with no `via` at all (older rows,
    webhooks) are all `(None, None, None)`."""
    via = (metadata or {}).get("via")
    if not isinstance(via, dict):
        return (None, None, None)
    return (via.get("kind"), via.get("key_id"), via.get("connection_id"))


def keeps_previous_body(metadata: Any) -> bool:
    """A description row that kept the previous body (an agent or API-key rewrite, MIP-738)."""
    return bool((metadata or {}).get("previousBodyKept"))


@dataclass
class Run:
    """`row` is the newest of `count` rows, the oldest of which was at `first_ts`."""

    row: Any
    count: int
    first_ts: datetime


def fold_description_runs(
    rows: Iterable[T],
    *,
    is_description: Callable[[T], bool],
    actor: Callable[[T], Any],
    via: Callable[[T], tuple[Any, Any, Any]],
    keeps_body: Callable[[T], bool],
    ts: Callable[[T], datetime],
) -> list[Run]:
    """`rows` (newest first) with every run of description edits folded into one `Run`, in order.
    A row that is not part of any run is a `Run` with `count == 1`."""
    out: list[Run] = []
    # State of the open run (the last entry of `out`): who wrote it, how, and the time of the
    # latest (oldest so far) row added to it. `None` when no run is open.
    open_actor: Any = None
    open_via: tuple[Any, Any, Any] = (None, None, None)
    open_prev_ts: datetime | None = None
    is_open = False
    for row in rows:
        folds = is_description(row) and not keeps_body(row)
        if (
            folds
            and is_open
            and actor(row) == open_actor
            and via(row) == open_via
            and open_prev_ts is not None
            and (open_prev_ts - ts(row)) <= DESCRIPTION_EDIT_GROUP_WINDOW
        ):
            out[-1].count += 1
            out[-1].first_ts = ts(row)
            open_prev_ts = ts(row)
            continue
        out.append(Run(row=row, count=1, first_ts=ts(row)))
        if folds:
            is_open, open_actor, open_via, open_prev_ts = True, actor(row), via(row), ts(row)
        else:
            is_open, open_actor, open_via, open_prev_ts = False, None, (None, None, None), None
    return out
