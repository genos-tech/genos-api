"""`list_calendar_events` tool — list events in a date range.

Calls Google Calendar v3 `events.list` with `singleEvents=true` so
recurring events expand into individual instances. Results are sorted
by start time. Free-form text (summary, description) is wrapped via
`wrap_workspace_content` inside `shape_event`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

from origin.search_engine.agent.calendar import (
    PARITY_ACCOUNT_PROPERTIES as PARITY_PROPERTIES,
)
from origin.search_engine.agent.calendar import (
    account_arg,
    account_fields,
    calendar_api_error,
    cite_event,
    google_calendar_request,
    member_google_accounts,
    read_from_account,
    resolve_google_calendar_account,
    segment,
    shape_event,
)
from origin.search_engine.agent.parity import followups_on, parity_on
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError

#: Appended to the description while the flag is on (`tool_flag_text`).
DESCRIPTION_ADDITION = (
    " Reads ONE account: the default unless `account` is given; `other_accounts` says how many more "
    "the member has."
)

#: The value of `account` that reads every connected Google account in one call (MIP-1607,
#: `AGENT_PARITY_FOLLOWUPS`). Not an account id, so it can never be mistaken for one.
ALL_ACCOUNTS = "all"

#: With the follow-ups flag on, the sentence that follows the parity one (`tool_flag_text`): the
#: model learns how many accounts there are from `list_calendars` (`accounts`) or from `other_accounts`
#: on a read, never from an extra call this sentence asks for.
FOLLOWUPS_DESCRIPTION_ADDITION = (
    " With several accounts (`accounts` of list_calendars, or `other_accounts`), pass `account` "
    '"all" to read every one in one call.'
)

#: The `account` property worded for the flag (`tool_flag_params`, `descriptions`).
FOLLOWUPS_ACCOUNT_DESCRIPTION = (
    'account_id from list_calendars, or "all" for every account. Omit for the default account.'
)

#: What a read of the default account says when the member has more accounts (dynamic, per call, with
#: `other_accounts`), so a model that did not look first learns the way to the rest in the result.
OTHER_ACCOUNTS_HINT = 'Only the default account was read. `account: "all"` reads every account.'

__all__ = [
    "ALL_ACCOUNTS",
    "FOLLOWUPS_ACCOUNT_DESCRIPTION",
    "FOLLOWUPS_DESCRIPTION_ADDITION",
    "LIST_CALENDAR_EVENTS",
    "PARITY_PROPERTIES",
]

# Upper bound on a single call; the model can paginate by adjusting
# `from`/`to` if it needs more.
_MAX_RESULTS_CAP = 100

#: `account: "all"` (MIP-1607): the most events one call returns over every account together, and the
#: most accounts it reads. Each account is read with `max_results` exactly as a single read is.
_ALL_EVENTS_CAP = 100
_ALL_ACCOUNTS_CAP = 10


def _read_params(args: dict[str, Any]) -> dict[str, Any]:
    """The query of one `events.list`: the same for a single account and for each account of "all"."""
    params: dict[str, Any] = {
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    time_min = args.get("from")
    time_max = args.get("to")
    if isinstance(time_min, str) and time_min:
        params["timeMin"] = time_min
    if isinstance(time_max, str) and time_max:
        params["timeMax"] = time_max

    raw_max = args.get("max_results")
    if raw_max is None:
        max_results = 50
    else:
        try:
            max_results = int(raw_max)
        except (TypeError, ValueError):
            raise ToolError(f"`max_results` must be an integer (got {raw_max!r}).")
    max_results = max(1, min(_MAX_RESULTS_CAP, max_results))
    params["maxResults"] = str(max_results)

    query = args.get("query")
    if isinstance(query, str) and query.strip():
        # Google's `q` is a full-text search over summary, description,
        # location, attendees. Useful for "find my standup".
        params["q"] = query.strip()
    return params


def _is_all(raw: Any) -> bool:
    return isinstance(raw, str) and raw.strip().lower() == ALL_ACCOUNTS


def _start_key(event: dict[str, Any]) -> datetime:
    """An event's start as an instant, to order events of different accounts (and time zones): an all-day
    date is its midnight in UTC, a time without an offset is read as UTC, and anything unreadable sorts last."""
    raw = event.get("start")
    try:
        if isinstance(raw, str) and "T" not in raw:
            return datetime.combine(date.fromisoformat(raw), time.min, tzinfo=UTC)
        parsed = datetime.fromisoformat(str(raw))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return datetime.max.replace(tzinfo=UTC)


def _run_all_accounts(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """`account: "all"`: every Google account of THE MEMBER, in one call (MIP-1607, flag on).

    The accounts are the member's own (`member_google_accounts` goes through the same `accounts_for` as every
    other account lookup), so there is no id to check and nothing of another member can be named. One that
    cannot be read does not hide the others: it is listed under `failed_accounts`, as `list_calendars` does;
    every account failing is an error with the first one's words. Each event carries its `account_id` (and the
    `cite` token that names it), and the events of all accounts come back in the order of their start."""
    _user, everything = member_google_accounts(ctx.user_id)
    calendar_id = args.get("calendar_id") or "primary"
    params = _read_params(args)
    path = f"/calendars/{segment(calendar_id)}/events"
    read, failed = [], []
    rows: list[tuple[datetime, int, dict[str, Any]]] = []
    for account in everything[:_ALL_ACCOUNTS_CAP]:
        body, failure = read_from_account(account, path, params)
        if failure is not None:
            failed.append(failure)
            continue
        shaped = [
            cite_event({**shape_event(e), "account_id": str(account.id)}, account.id, ctx)
            for e in (body or {}).get("items", [])
            if isinstance(e, dict)
        ]
        read.append({"account_id": str(account.id), "label": account.label, "events": len(shaped)})
        rows.extend((_start_key(e), len(read), e) for e in shaped)
    for account in everything[_ALL_ACCOUNTS_CAP:]:
        failed.append({"account_id": str(account.id), "reason": "Not read: too many accounts"})
    if failed and not read:
        raise ToolError(f"No events could be read: {failed[0]['reason']}.")
    rows.sort(key=lambda row: (row[0], row[1]))
    events = [row[2] for row in rows[:_ALL_EVENTS_CAP]]
    out: dict[str, Any] = {
        "calendar_id": calendar_id,
        "from": args.get("from"),
        "to": args.get("to"),
        "account": ALL_ACCOUNTS,
        "accounts": read,
        "events": events,
        "__summary__": f"Listed {len(events)} event(s) on `{calendar_id}` over {len(read)} account(s).",
    }
    if len(rows) > len(events):
        out["events_not_shown"] = len(rows) - len(events)
    if failed:
        out["failed_accounts"] = failed
    return out


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    account_ref = account_arg(args, write=False)
    if account_ref is not None and followups_on() and _is_all(account_ref):
        return _run_all_accounts(args, ctx)
    account = resolve_google_calendar_account(ctx.user_id, account_ref)
    calendar_id = args.get("calendar_id") or "primary"
    params = _read_params(args)
    time_min = args.get("from")
    time_max = args.get("to")

    resp = google_calendar_request(
        account, "GET", f"/calendars/{segment(calendar_id)}/events", params=params
    )
    if not resp.ok:
        raise calendar_api_error(resp, "list events")
    data = resp.json() or {}
    events = [shape_event(e) for e in data.get("items", []) if isinstance(e, dict)]
    result: dict[str, Any] = {
        "calendar_id": calendar_id,
        "from": time_min,
        "to": time_max,
        "events": events,
        "__summary__": f"Listed {len(events)} event(s) on `{calendar_id}`.",
    }
    if parity_on():
        result["events"] = [cite_event(e, account.id, ctx) for e in events]
        result.update(account_fields(account, defaulted=not account_ref, label=True))
        if followups_on() and result.get("other_accounts"):
            result["hint"] = OTHER_ACCOUNTS_HINT
    return result


LIST_CALENDAR_EVENTS = Tool(
    name="list_calendar_events",
    description=(
        "List Google Calendar events in a date range. Use this when the "
        "user asks 'what's on my calendar', 'do I have anything Friday', "
        "'next meeting', 'find my standup'. Recurring events are expanded "
        "into single instances. Returns id, summary, start/end (ISO), "
        "attendees, hangout_link, html_link, status. Pass `query` for "
        "full-text search across summary/description/location/attendees. "
        "Defaults to the user's primary calendar."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "from": {
                "type": "STRING",
                "description": (
                    "Start of range as ISO 8601, e.g. "
                    "'2026-05-22T00:00:00Z'. Optional — omit for events "
                    "from the start of recorded time."
                ),
            },
            "to": {
                "type": "STRING",
                "description": (
                    "End of range as ISO 8601, e.g. "
                    "'2026-05-29T23:59:59Z'. Optional — omit for no upper "
                    "bound."
                ),
            },
            "query": {
                "type": "STRING",
                "description": (
                    "Optional full-text search over event summary, "
                    "description, location, and attendees."
                ),
            },
            "calendar_id": {
                "type": "STRING",
                "description": (
                    "Google calendar id. Omit for the user's primary "
                    "calendar; resolve via `list_calendars` for others."
                ),
            },
            "max_results": {
                "type": "INTEGER",
                "description": (
                    f"Cap the number of events returned. Default 50, max {_MAX_RESULTS_CAP}."
                ),
            },
        },
        "required": [],
    },
    run=_run,
)
