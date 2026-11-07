"""The arguments the schedule tools share (MIP-1151): the recurrence, the job's parameters,
and one place that turns a schedule service error into a tool error the model can read."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from origin.search_engine.agent.tools import ToolError
from origin.services import schedules
from origin.services.user_time import user_zone

RECURRENCE_PROPERTIES: dict[str, Any] = {
    "frequency": {
        "type": "STRING",
        "enum": ["daily", "weekdays", "weekly", "monthly", "once"],
        "description": (
            "daily = every day; weekdays = Monday to Friday; weekly = the days in "
            "`weekdays`; monthly = the day in `month_day`; once = on `run_date`."
        ),
    },
    "time_of_day": {
        "type": "STRING",
        "description": "Local time in the member's own time zone, 24-hour, such as 09:00.",
    },
    "weekdays": {
        "type": "ARRAY",
        "items": {"type": "INTEGER"},
        "description": "For weekly: numbers 0 (Monday) to 6 (Sunday), such as [0, 2] for Monday and Wednesday.",
    },
    "month_day": {
        "type": "INTEGER",
        "description": "For monthly: 1 to 31. A month without that day runs on its last day.",
    },
    "run_date": {
        "type": "STRING",
        "description": "For once: the day, YYYY-MM-DD, in the member's time zone.",
    },
}


def job_params_schema() -> dict[str, Any]:
    """The job's parameters as an object. Which ones exist depends on the job and the jobs
    are code that can change in a deploy, so the schema does not freeze them: the catalog
    (`list_my_schedules`, `jobs`) lists them for each job, and the service refuses an unknown
    or out-of-range one by name, so a wrong guess is told what is allowed."""
    return {
        "type": "OBJECT",
        "properties": {},
        "description": (
            "The job's parameters by name, as listed for that job in `jobs` of "
            '`list_my_schedules`, such as {"day": "yesterday"}. Leave out what the '
            "member did not ask for: each has a default."
        ),
    }


def recurrence_args(args: dict[str, Any]) -> dict[str, Any]:
    return {k: args[k] for k in RECURRENCE_PROPERTIES if k in args}


def guarded(call: Callable[[], Any]) -> Any:
    """Run a schedule service call; its own errors are written for the member, so they are
    passed on as the tool's error and nothing else is."""
    try:
        return call()
    except schedules.ScheduleError as exc:
        raise ToolError(str(exc)) from None


__all__ = [
    "RECURRENCE_PROPERTIES",
    "guarded",
    "job_params_schema",
    "recurrence_args",
    "user_zone",
]
