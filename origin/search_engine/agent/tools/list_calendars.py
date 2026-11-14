"""`list_calendars` tool — discover the user's Google calendars.

Returns id, summary, whether-primary, and color so the model can pick
a non-primary calendar in follow-up calls. Most users only have one
primary calendar; this tool's value is for users with separate work
and personal calendars who want to schedule on a specific one.
"""

from __future__ import annotations

from typing import Any

import requests

from origin.search_engine.agent.calendar import (
    CALENDAR_EVENTS_SCOPE,
    account_arg,
    calendar_api_error,
    google_calendar_request,
    member_google_accounts,
    named_google_account,
    resolve_google_calendar_account,
)
from origin.search_engine.agent.parity import parity_on
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError

#: What `AGENT_PARITY_TOOLS` adds to this tool's schema (`tool_flag_params`).
PARITY_PROPERTIES: dict[str, dict[str, Any]] = {
    "account": {
        "type": "STRING",
        "description": "Only this account_id's calendars. Omit for every connected account.",
    },
}

#: Appended to the description while the flag is on (`tool_flag_text`).
DESCRIPTION_ADDITION = (
    " Lists EVERY connected Google account's calendars (`calendars[].account_id`; `accounts` has the "
    "address of each): pass an `account_id` as `account` to the other calendar tools to use that one."
)


def _calendar_row(c: dict[str, Any], account_id: str) -> dict[str, Any]:
    return {
        "id": c.get("id"),
        "account_id": account_id,
        "summary": c.get("summary"),
        "primary": bool(c.get("primary", False)),
        "background_color": c.get("backgroundColor"),
        "access_role": c.get("accessRole"),
    }


def _run_accounts(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """The flag-on read: every connected Google account's calendars, or the one named (MIP-1059).

    One account that cannot be read (its Google sign-in expired, Calendar was never granted) does
    not hide the others, as in the app's own picker; it is listed under `failed_accounts`. Every
    account failing is an error, with the first one's words."""
    user, everything = member_google_accounts(ctx.user_id)
    named = account_arg(args, write=False)
    # The same guard and the same words as every other calendar tool.
    accounts = [named_google_account(user, named)] if named else everything
    calendars: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for account in accounts:
        if CALENDAR_EVENTS_SCOPE not in (account.scopes or []):
            failed.append({"account_id": str(account.id), "reason": "Calendar access not granted"})
            continue
        try:
            resp = google_calendar_request(account, "GET", "/users/me/calendarList")
        except ToolError:
            # A sign-in Google no longer honours (`google_calendar_request`).
            failed.append({"account_id": str(account.id), "reason": "Google access expired"})
            continue
        except requests.RequestException:
            failed.append({"account_id": str(account.id), "reason": "Google could not be reached"})
            continue
        if not resp.ok:
            failed.append(
                {
                    "account_id": str(account.id),
                    "reason": f"Google answered HTTP {resp.status_code}",
                }
            )
            continue
        for c in (resp.json() or {}).get("items", []):
            if c.get("id"):
                calendars.append(_calendar_row(c, str(account.id)))
    if failed and not calendars:
        raise ToolError(f"No calendar could be read: {failed[0]['reason']}.")
    default_id = str(everything[0].id)
    out: dict[str, Any] = {
        "accounts": [
            {"account_id": str(a.id), "label": a.label, "default": str(a.id) == default_id}
            for a in accounts
        ],
        "calendars": calendars,
        "__summary__": f"Listed {len(calendars)} calendar(s) on {len(accounts)} account(s).",
    }
    if failed:
        out["failed_accounts"] = failed
    return out


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if parity_on():
        return _run_accounts(args, ctx)
    account = resolve_google_calendar_account(ctx.user_id)
    resp = google_calendar_request(account, "GET", "/users/me/calendarList")
    if not resp.ok:
        raise calendar_api_error(resp, "list calendars")
    data = resp.json() or {}
    calendars = [
        {
            "id": c.get("id"),
            "summary": c.get("summary"),
            "primary": bool(c.get("primary", False)),
            "background_color": c.get("backgroundColor"),
            "access_role": c.get("accessRole"),
        }
        for c in data.get("items", [])
        if c.get("id")
    ]
    return {
        "calendars": calendars,
        "__summary__": f"Listed {len(calendars)} calendar(s).",
    }


LIST_CALENDARS = Tool(
    name="list_calendars",
    description=(
        "List the user's Google Calendars (id, summary, whether it's the "
        "primary calendar, access_role). Use this when the user mentions "
        "a non-primary calendar by name ('work', 'personal', a shared "
        "team calendar) and you need its id to pass as `calendar_id` to "
        "a follow-up event tool. For most requests you can skip this and "
        "default `calendar_id` to 'primary'. Requires the user to have "
        "connected Google via Integrations."
    ),
    parameters_schema={"type": "OBJECT", "properties": {}, "required": []},
    run=_run,
)
