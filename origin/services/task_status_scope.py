"""`?statuses=` on the task list endpoints (MIP-1994).

The task table, the board and the milestone preview ask only for the statuses they show; the rest
(typically hundreds of Closed rows) is fetched when a filter first needs it. The answer also says
how many tasks each status has, so a chip can read "Closed (780)" without the rows.

A request without `statuses` is answered exactly as before (old clients, one release).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

STATUS_LABELS = ("Open", "WIP", "Blocked", "Pending", "Closed", "Deleted")


class BadStatuses(ValueError):
    pass


def parse_statuses(raw: str | None) -> frozenset[str] | None:
    """`None` when the caller sent no `statuses` (the old behaviour); else the set asked for.

    An unknown label is an error rather than a silent empty list, so a typo shows up at once."""
    if raw is None:
        return None
    labels = [part.strip() for part in raw.split(",") if part.strip()]
    if not labels:
        raise BadStatuses("statuses must name at least one status.")
    unknown = [label for label in labels if label not in STATUS_LABELS]
    if unknown:
        raise BadStatuses("Unknown status.")
    return frozenset(labels)


def count_statuses(statuses: Iterable[str | None]) -> dict[str, int]:
    """`{status: how many}` by the task's OWN status, every known label present (0 when none)."""
    found = Counter(s or "" for s in statuses)
    out = {label: found.get(label, 0) for label in STATUS_LABELS}
    for label, n in found.items():
        if label and label not in out:
            out[label] = n
    return out


def scope_rows(rows: list[dict], wanted: frozenset[str]) -> list[dict]:
    """The rows whose own status is wanted, plus what the client needs to read them.

    Kept besides the wanted rows: every ancestor (so a shown task under a Closed parent still has
    its tree and the client's closed-by-ancestor rule still sees the parent) and every milestone
    backing row (the milestone sidebar and scope filter are built from them)."""
    by_id = {str(r["id"]): r for r in rows}
    keep = {str(r["id"]) for r in rows if r["status"] in wanted or r.get("isMilestone")}
    for task_id in list(keep):
        parent = by_id[task_id].get("parentTaskId")
        seen = set()
        while parent is not None and str(parent) in by_id and str(parent) not in seen:
            seen.add(str(parent))
            keep.add(str(parent))
            parent = by_id[str(parent)].get("parentTaskId")
    return [r for r in rows if str(r["id"]) in keep]
