"""What the approval card of a calendar write says beyond what the model proposed (MIP-1059).

A paused `create_`, `update_` or `delete_calendar_event` showed the model's arguments as they were:
an `event_id` that means nothing to the person approving, no account, and (for a change) no idea what
the event is now. While `AGENT_PARITY_TOOLS` is on the card's `arguments` gain the facts a real card
needs, as DATA. Nothing here is a sentence: the client words them in the member's language, so no
English copy lives on the server (the MIP-929 rule).

Added to `arguments` (the wire event only; the stored arguments stay what the model sent):

  * `account_label`     the address of the Google account the write will go to (the default one when
                        `account` is not given), or null when `account` is not one of the member's;
  * `account_default`   true when the call names no account;
  * `account_count`     how many Google accounts the member has (a card shows the account line when
                        there is more than one);
  * `member_time_zone`  the member's own IANA zone: the times the model proposed carry no offset, and
                        this is the zone the card shows them in when `time_zone` is not given;
  * `current`           for an update or a delete: the event as it is NOW, read from Google as the
                        member (`start_iso`, `end_iso`, `all_day`, `time_zone`, `summary`, `location`,
                        `description` (cut), `attendees` (addresses), `add_meet`), so the card can say
                        which event this is and, for an update, what changes. `event_found: false`
                        when Google says there is no such event on that account and calendar.

The read is made ONLY at the pause (`controller._friendly_arguments(card=True)`), once, and it is best
effort: an account that cannot be read, a network error or a slow answer leave `current` off and the
card is shown without it. It is made with the account the write would use, after the ownership check
every calendar tool makes (`calendar.named_google_account`), so an account id that is not the member's
reads nothing.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from apis.request_id import describe_upstream
from origin.models.common.user_models import ConnectedAccount, CustomUser
from origin.search_engine.agent.calendar import (
    ACCOUNT_ARG,
    CALENDAR_EVENTS_SCOPE,
    google_calendar_request,
    named_google_account,
    segment,
)
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.services.oauth.accounts import accounts_for
from origin.services.user_time import zone_for_user_id

log = logging.getLogger(__name__)

CALENDAR_WRITE_TOOLS = frozenset(
    {"create_calendar_event", "update_calendar_event", "delete_calendar_event"}
)
#: The two that act on an event that exists.
_ON_AN_EVENT = frozenset({"update_calendar_event", "delete_calendar_event"})

#: Seconds the read may take: the card waits for it.
READ_TIMEOUT = 5
#: What of a description and of the guest list reaches a card.
DESCRIPTION_CHARS = 1000
ATTENDEES_CAP = 50


def _target_account(
    user: CustomUser, accounts: list[ConnectedAccount], named: Any
) -> ConnectedAccount | None:
    if named in (None, ""):
        return accounts[0] if accounts else None
    try:
        return named_google_account(user, named)
    except ToolError:
        return None


def _current_event(account: ConnectedAccount, args: dict[str, Any]) -> dict[str, Any] | None:
    """The event as Google has it now, shaped for the card; `{}` when there is no such event; None
    when it could not be read."""
    event_id = str(args.get("event_id") or "").strip()
    if not event_id or CALENDAR_EVENTS_SCOPE not in (account.scopes or []):
        return None
    calendar_id = args.get("calendar_id") or "primary"
    try:
        resp = google_calendar_request(
            account,
            "GET",
            f"/calendars/{segment(calendar_id)}/events/{segment(event_id)}",
            timeout=READ_TIMEOUT,
        )
    except (ToolError, requests.RequestException):
        # A sign-in Google no longer honours, or no answer: no card detail, never a failed card. No
        # exception text: its URL carries the calendar id, which is usually an address.
        return None
    if resp.status_code == 404:
        return {}
    if not resp.ok:
        log.warning("calendar card read failed: %s", describe_upstream(resp))
        return None
    raw = resp.json() or {}
    start, end = raw.get("start") or {}, raw.get("end") or {}
    attendees = [
        a["email"]
        for a in raw.get("attendees") or []
        if isinstance(a, dict) and isinstance(a.get("email"), str)
    ]
    return {
        "summary": raw.get("summary") or "",
        "start_iso": start.get("date") or start.get("dateTime"),
        "end_iso": end.get("date") or end.get("dateTime"),
        "all_day": "date" in start,
        "time_zone": start.get("timeZone"),
        "location": raw.get("location") or None,
        "description": (raw.get("description") or "")[:DESCRIPTION_CHARS] or None,
        "attendees": attendees[:ATTENDEES_CAP],
        "add_meet": bool(raw.get("hangoutLink") or raw.get("conferenceData")),
    }


def card_facts(tool_name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """The keys to add to the card's `arguments` for a calendar write; `{}` for any other tool or
    when the member cannot be found. Never raises for a missing account or a Google failure."""
    if tool_name not in CALENDAR_WRITE_TOOLS:
        return {}
    user = CustomUser.objects.filter(id=ctx.user_id).first()
    if user is None:
        return {}
    accounts = list(accounts_for(user, "google"))
    named = args.get(ACCOUNT_ARG)
    account = _target_account(user, accounts, named)
    facts: dict[str, Any] = {
        "account_label": account.label if account is not None else None,
        "account_default": named in (None, ""),
        "account_count": len(accounts),
        "member_time_zone": zone_for_user_id(ctx.user_id).key,
    }
    if tool_name in _ON_AN_EVENT and account is not None:
        current = _current_event(account, args)
        if current == {}:
            facts["event_found"] = False
        elif current is not None:
            facts["current"] = current
    return facts
