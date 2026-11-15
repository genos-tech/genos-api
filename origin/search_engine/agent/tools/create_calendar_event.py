"""`create_calendar_event` write tool.

Creates a new event on the user's calendar. REQUIRES USER APPROVAL —
the agent controller routes the call through the pause/resume protocol
so the user can review the proposed event before it gets posted to
Google.

Time handling:
  - `start_iso`/`end_iso` accept "YYYY-MM-DD" (all-day) or
    "YYYY-MM-DDTHH:MM:SS[Z|±HH:MM]" (timed). Both must use the same
    shape — Google rejects mixed all-day/timed events.
  - For all-day events, Google treats `end` as exclusive. A single-day
    event on 2026-06-30 sets `end_iso` to '2026-07-01'.

Meet:
  - `add_meet=true` attaches a `createRequest`. Google generates the
    `hangoutLink` asynchronously; the response may include it inline
    OR with `conferenceData.createRequest.status.statusCode=pending`.
    Callers needing the link reliably should poll the event after
    creation (the frontend Quick Meet flow already does this).
"""

from __future__ import annotations

import uuid
from typing import Any

from origin.search_engine.agent.calendar import (
    PARITY_ACCOUNT_PROPERTIES as PARITY_PROPERTIES,
)
from origin.search_engine.agent.calendar import (
    account_arg,
    account_fields,
    build_time_endpoint,
    calendar_api_error,
    cite_event,
    effective_time_zone,
    emails_to_attendees,
    google_calendar_request,
    resolve_google_calendar_account,
    segment,
    shape_event,
    validate_time_pair,
)
from origin.search_engine.agent.parity import parity_on
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError

__all__ = ["CREATE_CALENDAR_EVENT", "DESCRIPTION_ADDITION", "PARITY_PROPERTIES"]

#: Appended to the description while `AGENT_PARITY_TOOLS` is on (`tool_flag_text`). A write proposes before it
#: reads anything, so the default account is what it gets unless the model looks the account up first. The paid
#: run of 2026-11-15 had a Japanese "personal Google calendar" request proposed on the default (work) account,
#: with no `account` and no `list_calendars`, in 2 of 3 repetitions (the English twin looked it up 3 of 3).
#: Conditional on the member NAMING one, so a member with one account, who names none, is sent nowhere. The
#: wording is the same for every language on purpose, and short: the prefix has little headroom.
DESCRIPTION_ADDITION = (
    " When the member names a calendar or an account (personal, work, an address; any language), "
    "call `list_calendars` first and pass that `account`: the default is the first one connected. "
    "Names none: leave `account` out."
)


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    summary = (args.get("summary") or "").strip()
    if not summary:
        raise ToolError("`summary` is required.")
    start_iso = (args.get("start_iso") or "").strip()
    end_iso = (args.get("end_iso") or "").strip()
    if not start_iso or not end_iso:
        raise ToolError("`start_iso` and `end_iso` are required.")
    validate_time_pair(start_iso, end_iso)

    time_zone = effective_time_zone(
        args.get("time_zone") or None, (start_iso, end_iso), ctx.user_id
    )
    calendar_id = args.get("calendar_id") or "primary"

    body: dict[str, Any] = {
        "summary": summary,
        "start": build_time_endpoint(start_iso, time_zone),
        "end": build_time_endpoint(end_iso, time_zone),
    }
    description = args.get("description")
    if isinstance(description, str) and description:
        body["description"] = description
    location = args.get("location")
    if isinstance(location, str) and location:
        body["location"] = location

    params: dict[str, Any] = {}
    attendees = emails_to_attendees(args.get("attendees"))
    if attendees:
        body["attendees"] = attendees
        # Match the frontend Quick Meet default — the chat / agent
        # response is the notification surface; emails on top would be
        # duplicate noise. Users can re-send invites from Calendar UI.
        params["sendUpdates"] = "none"

    if args.get("add_meet"):
        body["conferenceData"] = {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        # `conferenceDataVersion=1` is required for Google to honor the
        # createRequest; without it the field is silently dropped.
        params["conferenceDataVersion"] = 1

    account_ref = account_arg(args, write=True)
    account = resolve_google_calendar_account(ctx.user_id, account_ref)
    resp = google_calendar_request(
        account,
        "POST",
        f"/calendars/{segment(calendar_id)}/events",
        json=body,
        params=params or None,
    )
    if not resp.ok:
        raise calendar_api_error(resp, "create event")
    event = resp.json() or {}
    shaped = shape_event(event)
    result = {
        **shaped,
        "calendar_id": calendar_id,
        "__summary__": f"Created event '{summary}' on `{calendar_id}`.",
    }
    if parity_on():
        result = {
            **cite_event(result, account.id, ctx),
            **account_fields(account, defaulted=not account_ref),
        }
    return result


CREATE_CALENDAR_EVENT = Tool(
    name="create_calendar_event",
    description=(
        "Create a new Google Calendar event. REQUIRES USER APPROVAL — "
        "the user sees your proposed event and decides whether to "
        "execute. Use this when the user explicitly asks to schedule / "
        "add / create a calendar event or meeting. Required: summary, "
        "start_iso, end_iso. Pass dates as 'YYYY-MM-DD' for all-day "
        "events (end is EXCLUSIVE — single-day uses next day), or ISO "
        "datetime 'YYYY-MM-DDTHH:MM:SS' for timed events. Optional: "
        "description, location, time_zone, calendar_id, attendees "
        "(array of emails), add_meet (true to attach a Google Meet "
        "link). Ask the user for their timezone if you don't know it."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "summary": {
                "type": "STRING",
                "description": "Event title (one line).",
            },
            "start_iso": {
                "type": "STRING",
                "description": (
                    "Start of event. 'YYYY-MM-DD' for all-day events, "
                    "'YYYY-MM-DDTHH:MM:SS' for timed events. Must match "
                    "the shape of `end_iso`."
                ),
            },
            "end_iso": {
                "type": "STRING",
                "description": (
                    "End of event. Same shape rules as `start_iso`. For "
                    "all-day events, Google treats `end` as exclusive — "
                    "e.g. a single-day event on 2026-06-30 needs "
                    "end_iso '2026-07-01'."
                ),
            },
            "time_zone": {
                "type": "STRING",
                "description": (
                    "IANA timezone (e.g. 'America/Los_Angeles', "
                    "'Asia/Tokyo'). Used only for timed events. Ask the "
                    "user if unsure — guessing can shift meeting times."
                ),
            },
            "description": {
                "type": "STRING",
                "description": "Optional event description / agenda.",
            },
            "location": {
                "type": "STRING",
                "description": "Optional location string.",
            },
            "calendar_id": {
                "type": "STRING",
                "description": ("Google calendar id. Omit for the primary calendar."),
            },
            "attendees": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": (
                    "Email addresses to invite. Email notifications are "
                    "suppressed by default — the user can re-send invites "
                    "from Google Calendar if needed."
                ),
            },
            "add_meet": {
                "type": "BOOLEAN",
                "description": (
                    "true → attach a Google Meet link. The link is "
                    "generated asynchronously; the returned event may "
                    "show `hangout_link` immediately or after a short "
                    "delay."
                ),
            },
        },
        "required": ["summary", "start_iso", "end_iso"],
    },
    run=_run,
    requires_approval=True,
)
