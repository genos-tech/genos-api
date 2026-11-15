"""`get_calendar_event` tool: read ONE Google Calendar event (MIP-1059, flag `AGENT_PARITY_TOOLS`).

The calendar tools could list a range of events and change or delete one by id, but not look at one:
an id the member points at (a chip, a link, an earlier turn) had to be found again by a date range.
This reads it directly, on any of the member's connected Google accounts.

Filed with `register_flagged_tool`, so with the flag off it is declared, runnable and published nowhere.
Its `account` is part of its own schema (it exists only while the flag is on); on the other five
calendar tools the same parameter is laid over their schema by `tool_flag_params`.

An event is the member's own calendar data: the account is looked up among THEIR accounts
(`calendar.named_google_account`), so another member's account id finds nothing, and a calendar the
member is only subscribed to (a teammate's shared calendar) reads exactly as far as Google lets that
account read it. The summary and description are text anyone who invited the member could write: both
are fenced inside `shape_event`.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.calendar import (
    NOT_FOUND_ACCOUNT_HINT,
    PARITY_ACCOUNT_PROPERTIES,
    account_arg,
    account_fields,
    calendar_api_error,
    cite_event,
    google_calendar_request,
    resolve_google_calendar_account,
    segment,
    shape_event,
)
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    event_id = (args.get("event_id") or "").strip()
    if not event_id:
        raise ToolError("`event_id` is required.")
    calendar_id = args.get("calendar_id") or "primary"
    account_ref = account_arg(args, write=False)
    account = resolve_google_calendar_account(ctx.user_id, account_ref)
    resp = google_calendar_request(
        account, "GET", f"/calendars/{segment(calendar_id)}/events/{segment(event_id)}"
    )
    if resp.status_code == 404:
        raise ToolError(
            f"Event {event_id} not found on calendar `{calendar_id}`. It may have been deleted."
            + NOT_FOUND_ACCOUNT_HINT
        )
    if not resp.ok:
        raise calendar_api_error(resp, "read event")
    shaped = shape_event(resp.json() or {})
    return {
        **cite_event(shaped, account.id, ctx),
        "calendar_id": calendar_id,
        **account_fields(account, defaulted=not account_ref, label=True),
        "__summary__": f"Read event {event_id}.",
    }


GET_CALENDAR_EVENT = Tool(
    name="get_calendar_event",
    description=(
        "Read ONE Google Calendar event by its `event_id`: title, description, start/end (ISO), "
        "attendees, links, status. Use it to look at an event the member points at, and before "
        "changing or deleting one. To find events by date or text use `list_calendar_events`. Pass "
        "the `calendar_id` and `account` the event came with; omitted, it reads the default "
        "account's primary calendar."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "event_id": {"type": "STRING", "description": "Google Calendar event id."},
            "calendar_id": {
                "type": "STRING",
                "description": "Calendar the event is on. Omit for the primary calendar.",
            },
            **PARITY_ACCOUNT_PROPERTIES,
        },
        "required": ["event_id"],
    },
    run=_run,
)
