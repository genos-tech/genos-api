"""How a member's schedule reads to a person or to a connected AI app (MIP-1151).

One place for the words and the shape, so the MCP tools (now) and the Schedules page
(MIP-1150) say the same thing: "every Monday and Wednesday at 09:00", the next run in the
member's own zone, what the last run did. Pure formatting: no query, no write.
"""

from __future__ import annotations

from typing import Any

from origin.models.common.schedule_models import (
    FREQ_DAILY,
    FREQ_MONTHLY,
    FREQ_ONCE,
    FREQ_WEEKDAYS,
    FREQ_WEEKLY,
    MemberSchedule,
)
from origin.services.schedule_jobs import JOBS

_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _list_in_words(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def describe_recurrence(sched: MemberSchedule) -> str:
    at = f"{sched.time_of_day:%H:%M}"
    if sched.frequency == FREQ_DAILY:
        return f"every day at {at}"
    if sched.frequency == FREQ_WEEKDAYS:
        return f"every Monday to Friday at {at}"
    if sched.frequency == FREQ_WEEKLY:
        days = [_DAY_NAMES[d] for d in sorted(sched.weekdays or []) if 0 <= d <= 6]
        return f"every {_list_in_words(days)} at {at}"
    if sched.frequency == FREQ_MONTHLY:
        return f"on the {_ordinal(sched.month_day or 1)} of every month at {at}"
    if sched.frequency == FREQ_ONCE:
        return f"once, on {sched.run_date.isoformat() if sched.run_date else '?'} at {at}"
    return at


def _local(value, zone) -> str | None:
    return value.astimezone(zone).isoformat(timespec="minutes") if value else None


def present(sched: MemberSchedule, zone) -> dict[str, Any]:
    """The schedule as a plain dict. Times are in `zone` (the member's own). The report a
    run made is not here (it is in the Inbox); only its one-line summary."""
    job = JOBS.get(sched.job)
    out: dict[str, Any] = {
        "schedule_id": str(sched.id),
        "name": sched.name,
        "job": sched.job,
        "job_title": job.title if job else None,
        "params": sched.params or {},
        "when": describe_recurrence(sched),
        "frequency": sched.frequency,
        "time_of_day": f"{sched.time_of_day:%H:%M}",
        "enabled": sched.enabled,
        "deliver_push": sched.deliver_push,
        "next_run": _local(sched.next_run_at, zone),
        "last_run": _local(sched.last_run_at, zone),
        "last_status": sched.last_status or None,
    }
    if sched.frequency == FREQ_WEEKLY:
        out["weekdays"] = list(sched.weekdays or [])
    if sched.frequency == FREQ_MONTHLY:
        out["month_day"] = sched.month_day
    if sched.frequency == FREQ_ONCE and sched.run_date:
        out["run_date"] = sched.run_date.isoformat()
    if not sched.enabled and sched.paused_reason:
        out["paused_reason"] = sched.get_paused_reason_display()
    if job is not None and job.uses_ai:
        # The AI effort (MIP-1212), only for a job that uses AI: what the member chose ("" = the
        # job's own default) and the level a run will use (the choice or the default, held to the
        # plan's ceiling). A job with no AI has neither: there is nothing to choose.
        out["ai_effort"] = sched.ai_effort or None
        out["ai_effort_effective"] = effective_effort(sched, job)
    if sched.last_error:
        out["last_error"] = sched.last_error
    if sched.last_result:
        out["last_result"] = sched.last_result
    if job is None:
        out["problem"] = "This job no longer exists; delete the schedule or choose another job."
    return out


def effective_effort(sched: MemberSchedule, job) -> str:
    """The level a run of this schedule will use: its own choice if the catalog still has it,
    else the job's default, held to the member's plan ceiling (never an error)."""
    from origin.services.report_ai import effort as effort_mod  # noqa: PLC0415

    run = effort_mod.resolve(sched.ai_effort, job.effort_default, sched.user_id, clamp=True)
    return (
        run.effort if run else (effort_mod.valid(job.effort_default) or effort_mod.FALLBACK_EFFORT)
    )


def _effort_cost(job) -> dict[str, Any] | None:
    """What one run costs at each level: an ESTIMATE from the size of the real prompt and the
    catalog's prices (`report_ai.estimate`), never a measurement. None for a job with no AI."""
    if not job.uses_ai:
        return None
    from origin.services.report_ai import effort as effort_mod  # noqa: PLC0415
    from origin.services.report_ai import estimate  # noqa: PLC0415

    levels = {}
    for level in effort_mod.EFFORTS:
        row = estimate.estimate(level)
        if row is not None:
            levels[level] = {
                "model": row["model"],
                "credits_milli": row["credits_milli"],
                "usd_micro": row["usd_micro"],
            }
    return {
        "estimate": True,
        "basis": "a report listing 10 tasks with comments, nothing cached; each range is [fewest, most]",
        "levels": levels,
    }


def job_catalog() -> list[dict[str, Any]]:
    """What a schedule can run: key, title, what it does and the parameters it takes, and
    (MIP-1212) whether it uses AI, the effort it defaults to and what a run costs at each level."""
    return [
        {
            "job": job.key,
            "title": job.title,
            "description": job.description,
            "params": job.params_schema.get("properties", {}),
            "uses_ai": job.uses_ai,
            "effort_default": job.effort_default if job.uses_ai else None,
            "effort_cost": _effort_cost(job),
        }
        for job in sorted(JOBS.values(), key=lambda j: j.key)
    ]
