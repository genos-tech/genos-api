"""The schedules Genos suggests to a member (MIP-1152).

Not a fixed workflow and not created behind anyone's back: they are two ORDINARY schedules a
member is offered once they look (the Schedules page, or `list_my_schedules` over MCP) and
accepts with one step, after which they are theirs to edit, pause or delete like any other.
A suggestion is left out when the member already has a schedule that is the same (same job,
same time, same days), so accepting one makes it disappear and a member who made their own
is not offered a copy. Deleting an accepted one offers it again; that is the cost of not
keeping a "seen" marker (and a table for it).
"""

from __future__ import annotations

from typing import Any

from origin.models.common.schedule_models import MemberSchedule
from origin.services.schedule_jobs import JOBS

SUGGESTED: tuple[dict[str, Any], ...] = (
    {
        "key": "morning_plan",
        "name": "Morning plan",
        "job": "what_to_do_today",
        "params": {},
        "frequency": "daily",
        "time_of_day": "09:00",
    },
    {
        "key": "evening_recap",
        "name": "Evening recap",
        "job": "what_you_did",
        "params": {"day": "today"},
        "frequency": "daily",
        "time_of_day": "17:00",
    },
)


def _same(suggestion: dict[str, Any], sched: MemberSchedule) -> bool:
    return (
        sched.job == suggestion["job"]
        and sched.frequency == suggestion["frequency"]
        and f"{sched.time_of_day:%H:%M}" == suggestion["time_of_day"]
    )


def suggestions_for(existing: list[MemberSchedule]) -> list[dict[str, Any]]:
    """The suggestions this member does not already have, whose job still exists."""
    return [
        dict(s)
        for s in SUGGESTED
        if s["job"] in JOBS and not any(_same(s, sched) for sched in existing)
    ]
