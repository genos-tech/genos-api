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


AI_EFFORT_PROPERTY: dict[str, Any] = {
    "type": "STRING",
    "enum": ["low", "medium", "high"],
    "description": (
        "For a job that uses AI (`uses_ai` in `jobs`): how much effort it spends, low, medium or "
        "high. Leave it out for the job's own `effort_default`. Ignored by a job that uses no AI."
    ),
}


DELIVER_CHAT_PROPERTY: dict[str, Any] = {
    "type": "STRING",
    "description": (
        "Post the report in this chat instead of the Inbox: a `chat_id` from `list_my_chats`. "
        "The service refuses a chat the report may not go to and says why. On an update, an "
        "empty string goes back to the Inbox."
    ),
}
SAVE_TO_PROPERTY: dict[str, Any] = {
    "type": "STRING",
    "enum": ["me", "personal_folder", "team_folder"],
    "description": (
        "For `ai_dashboard`: where its note is saved, me (default), personal_folder or "
        "team_folder (both need `folder_id`). Fixed once the first note exists."
    ),
}
FOLDER_ID_PROPERTY: dict[str, Any] = {
    "type": "INTEGER",
    "description": "For `ai_dashboard` with a folder in `save_to`: the id from `list_note_folders`.",
}
PLACEMENT_PROPERTIES: dict[str, Any] = {
    "deliver_chat_id": DELIVER_CHAT_PROPERTY,
    "save_to": SAVE_TO_PROPERTY,
    "folder_id": FOLDER_ID_PROPERTY,
}


def placement_args(args: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """The chat and the note place as arguments of the schedule service: `deliver_chat_id` is
    the service's `deliver_channel_id`; `save_to` and `folder_id` are job parameters, so they are
    merged into `params` (the schedule's own, when only they change) and the job's own check
    decides. The same value given twice with a different meaning is refused, not guessed."""
    out: dict[str, Any] = {}
    if "deliver_chat_id" in args:
        out["deliver_channel_id"] = args["deliver_chat_id"] or None
    place = {k: args[k] for k in ("save_to", "folder_id") if k in args}
    if place:
        given = args.get("params")
        if given is not None and not isinstance(given, dict):
            raise ToolError("`params` must be an object.")
        for key, value in place.items():
            if given and key in given and given[key] != value:
                raise ToolError(f"`{key}` is given twice with different values.")
        base = given if given is not None else (current or {})
        out["params"] = {**base, **place}
    return out


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
    "AI_EFFORT_PROPERTY",
    "PLACEMENT_PROPERTIES",
    "placement_args",
    "RECURRENCE_PROPERTIES",
    "guarded",
    "job_params_schema",
    "recurrence_args",
    "user_zone",
]
