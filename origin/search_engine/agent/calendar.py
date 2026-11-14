"""Shared helpers for the Google Calendar agent tools.

Five tools (`list_calendars`, `list_calendar_events`,
`create_calendar_event`, `update_calendar_event`, `delete_calendar_event`)
share the same auth check, request wrapper, and event-shaping logic.
Pulling it here keeps each tool file focused on its argument schema.

Design notes:
  - `resolve_google_calendar_account` raises `ToolError` directly so the
    model sees an actionable message. "Google not connected" and
    "calendar scope missing" map to different remediation buttons in
    the UI, and the LLM's natural-language answer should reflect that.
  - `shape_event` is the canonical projection of a Google event into a
    flat dict the model can scan. Free-form fields (summary, description)
    are wrapped via `wrap_workspace_content` because a malicious event
    body could try to inject instructions.
  - `build_time_endpoint` accepts the model's "YYYY-MM-DD" (all-day) or
    full ISO datetime and produces Google's start/end object shape.
    Asking the model to construct Google's exact schema was brittle —
    half the time it'd pass `dateTime` with a bare date string.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any
from urllib.parse import quote

import requests

from apis.request_id import describe_upstream
from origin.models.common.user_models import ConnectedAccount, CustomUser
from origin.search_engine.agent.parity import parity_on
from origin.search_engine.agent.tools.base import (
    ToolContext,
    ToolError,
    ToolForbidden,
    wrap_workspace_content,
)
from origin.services.oauth.accounts import accounts_for
from origin.services.oauth.tokens import ReauthRequired, get_valid_access_token

logger = logging.getLogger(__name__)

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
# Minimum scope for read AND write of calendar events. A user who only
# signed in with Google ("login" intent) has openid/email/profile and
# will hit upstream 403s on any Calendar v3 call — check up front so
# the model sees a clean signal instead of leaking the 403.
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"


#: The parameter the calendar tools take while `AGENT_PARITY_TOOLS` is on (MIP-1059): the
#: `account_id` of one of the member's connected Google accounts, as `list_calendars` lists it.
ACCOUNT_ARG = "account"

#: The ONE sentence for every "no" on a named account: a made-up id, an id that is another
#: member's, and a value that is not an id at all (MIP-1059). Nothing of the value is echoed (it can
#: be an e-mail address the model copied, and an error is logged), and nothing tells the three apart,
#: so an account id cannot be probed for existence.
ACCOUNT_REFUSAL = (
    "`account` is not one of this member's connected Google accounts. Call `list_calendars` and "
    "pass an `account_id` it lists, or leave `account` out for the default account."
)


#: What `AGENT_PARITY_TOOLS` adds to the schema of `create_`, `update_` and `delete_calendar_event` and
#: of `get_calendar_event` (`tool_flag_params`).
PARITY_ACCOUNT_PROPERTIES: dict[str, dict[str, Any]] = {
    ACCOUNT_ARG: {
        "type": "STRING",
        "description": "account_id from list_calendars. Omit for the default account.",
    },
}

#: Appended to a "not found" while the flag is on: with several accounts the likeliest cause.
NOT_FOUND_ACCOUNT_HINT = (
    " If the member has several Google accounts it may be on another one: pass its `account`."
)


def named_google_account(user: CustomUser, raw: Any) -> ConnectedAccount:
    """The member's OWN Google account whose id is `raw`, or a refusal (MIP-1059).

    The whole authorization check for `account`: the lookup is scoped to `user` (the same
    `accounts_for` the app's calendar endpoints resolve through), so an id that belongs to someone
    else finds nothing. The value is parsed as a UUID BEFORE any query: a UUID column raises
    a `ValidationError` that quotes the value, and the controller logs a tool's crash.

    An id that exists but is not the member's is a `ToolForbidden` (the MCP refusal alarm counts
    it, as it does for every "found but not yours"); a made-up one is a `ToolError`. The words are
    the same.
    """
    try:
        wanted = uuid.UUID(str(raw).strip())
    except (ValueError, AttributeError):
        raise ToolError(ACCOUNT_REFUSAL) from None
    account = accounts_for(user, "google").filter(id=wanted).first()
    if account is not None:
        return account
    if ConnectedAccount.objects.filter(id=wanted).exists():
        raise ToolForbidden(ACCOUNT_REFUSAL)
    raise ToolError(ACCOUNT_REFUSAL)


def member_google_accounts(user_id: str) -> tuple[CustomUser, list[ConnectedAccount]]:
    """The member and every Google account they have connected, the default (the first) leading.
    Raises the same ToolErrors `resolve_google_calendar_account` does for "no user" and "not
    connected"."""
    try:
        user = CustomUser.objects.get(id=user_id)
    except CustomUser.DoesNotExist:
        raise ToolError("Current user record not found.")
    accounts = list(accounts_for(user, "google"))
    if not accounts:
        raise ToolError(
            "Google is not connected for this user. Direct them to "
            "Settings → Integrations to connect Google."
        )
    return user, accounts


def account_arg(args: dict[str, Any], *, write: bool) -> str | None:
    """The `account` a call names, or None for "the default account" (MIP-1059).

    While `AGENT_PARITY_TOOLS` is off the parameter does not exist. A READ that carries one anyway
    (a model that invents it, an MCP client holding yesterday's tool list) ignores it, as an unknown
    argument always was. A WRITE refuses it: an approval paused while the flag was on says "personal
    account" on its card, and dropping the argument would put the event on the default account
    instead, silently, when the person approves after the flag went off.
    """
    raw = args.get(ACCOUNT_ARG)
    if raw is None or raw == "":
        return None
    if not parity_on():
        if write:
            raise ToolError(
                "Choosing a Google account is not available, so nothing was changed. "
                "Ask again without naming an account."
            )
        return None
    return str(raw)


def resolve_google_calendar_account(user_id: str, account: str | None = None) -> ConnectedAccount:
    """Resolve the user's Google ConnectedAccount, requiring calendar scope.

    `account` (MIP-1059, only ever passed while `AGENT_PARITY_TOOLS` is on) names one of the
    member's accounts by its id; without it the answer is exactly what it always was: the first.

    Raises ToolError on:
      - user not found (defensive — shouldn't happen post-auth)
      - Google not connected for the user
      - `account` is not one of the user's Google accounts (`named_google_account`)
      - account connected but missing the events scope
    """
    try:
        user = CustomUser.objects.get(id=user_id)
    except CustomUser.DoesNotExist:
        raise ToolError("Current user record not found.")

    if account:
        # "Not connected" comes first, so a member with no Google account is told that, and the
        # refusal for a bad id never says whether anyone has an account.
        if not ConnectedAccount.objects.filter(user=user, provider="google").exists():
            raise ToolError(
                "Google is not connected for this user. Direct them to "
                "Settings → Integrations to connect Google."
            )
        chosen = named_google_account(user, account)
    else:
        chosen = ConnectedAccount.objects.filter(user=user, provider="google").first()
    if chosen is None:
        raise ToolError(
            "Google is not connected for this user. Direct them to "
            "Settings → Integrations to connect Google."
        )
    if CALENDAR_EVENTS_SCOPE not in (chosen.scopes or []):
        raise ToolError(
            "Google is connected but Calendar access hasn't been granted. "
            "Direct the user to Settings → Tasks → Grant Calendar access "
            "(or the matching button in Integrations)."
        )
    return chosen


def google_calendar_request(
    account: ConnectedAccount,
    method: str,
    path: str,
    **kwargs: Any,
) -> requests.Response:
    """`requests` wrapper that injects the auto-refreshed access token."""
    try:
        token = get_valid_access_token(account)
    except ReauthRequired as exc:
        # Refresh token is revoked/expired — same actionable shape as the
        # not-connected / scope-missing ToolErrors in
        # `resolve_google_calendar_account`, so raise the same kind of
        # message rather than leaking a stack trace into the agent.
        raise ToolError(
            "Google Calendar access has expired. Direct the user to "
            "Settings → Integrations to reconnect Google."
        ) from exc
    headers = kwargs.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {token}"
    headers.setdefault("Accept", "application/json")
    timeout = kwargs.pop("timeout", 15)
    return requests.request(
        method, f"{CALENDAR_API_BASE}{path}", headers=headers, timeout=timeout, **kwargs
    )


def segment(value: Any) -> str:
    """A calendar id or an event id as ONE URL path segment, percent-encoded as the app's calendar
    endpoints do (`calendar_views._cal`). Ids are usually e-mail-like and pass through, but Google's
    holiday calendars carry a literal `#` ("en.japanese#holiday@group.v.calendar.google.com"), which
    interpolated raw starts a URL fragment and cuts the path, and a value with a `/` or `?` would
    reach another endpoint with the member's token. A value that is only dots survives the encoding
    and a client resolves it as a step up the path, so it is refused."""
    text = str(value)
    if text.strip(".") == "":
        raise ToolError("That is not a valid calendar or event id.")
    return quote(text, safe="")


def _is_date_only(value: str) -> bool:
    """True for "YYYY-MM-DD". False for ISO datetimes."""
    return isinstance(value, str) and len(value) == 10 and "T" not in value


def build_time_endpoint(value: str, time_zone: str | None) -> dict[str, Any]:
    """Turn a user-supplied ISO string into Google's start/end shape.

    - "YYYY-MM-DD" → all-day: {"date": "YYYY-MM-DD"}
    - "YYYY-MM-DDTHH:MM:SS[Z|±HH:MM]" → timed: {"dateTime": ..., "timeZone": ...}
    """
    if _is_date_only(value):
        return {"date": value}
    body: dict[str, Any] = {"dateTime": value}
    if time_zone:
        body["timeZone"] = time_zone
    return body


def validate_time_pair(start_iso: str, end_iso: str) -> None:
    """Reject mixed all-day/timed endpoint shapes; Google rejects them too."""
    if _is_date_only(start_iso) != _is_date_only(end_iso):
        raise ToolError(
            "`start_iso` and `end_iso` must both be date-only (YYYY-MM-DD) "
            "or both be ISO datetimes — Google rejects mixed shapes."
        )


def emails_to_attendees(raw: Any) -> list[dict[str, str]]:
    """Convert an LLM-supplied array of email strings into Google's
    attendees shape. Non-string entries and empty strings are dropped.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            out.append({"email": entry.strip()})
    return out


def shape_event(event: dict[str, Any]) -> dict[str, Any]:
    """Project a Google Calendar event into a flat, LLM-friendly dict."""
    start = event.get("start") or {}
    end = event.get("end") or {}
    attendees_raw = event.get("attendees") or []
    attendees = [
        {
            "email": a.get("email"),
            "displayName": a.get("displayName"),
            "responseStatus": a.get("responseStatus"),
        }
        for a in attendees_raw
        if isinstance(a, dict) and a.get("email")
    ]
    return {
        "id": event.get("id"),
        "summary": wrap_workspace_content(event.get("summary") or ""),
        "description": wrap_workspace_content(event.get("description") or ""),
        "location": event.get("location"),
        "start": start.get("date") or start.get("dateTime"),
        "end": end.get("date") or end.get("dateTime"),
        "all_day": "date" in start,
        "html_link": event.get("htmlLink"),
        "hangout_link": event.get("hangoutLink"),
        "attendees": attendees,
        "status": event.get("status"),
        "creator_email": (event.get("creator") or {}).get("email"),
        "organizer_email": (event.get("organizer") or {}).get("email"),
    }


def calendar_api_error(resp: requests.Response, context: str) -> ToolError:
    """Build a uniform ToolError from a non-OK calendar response.

    `context` is a short verb phrase like "create event" / "list events"
    that gets folded into the error message the model sees.
    """
    logger.warning("calendar tool %s failed: %s", context, describe_upstream(resp))
    return ToolError(f"Google Calendar {context} failed: HTTP {resp.status_code}.")


# --------------------------------------------------------------------------- #
# A calendar event as a source (MIP-1059)                                      #
# --------------------------------------------------------------------------- #

#: The `entity_type` of an event's chip and the prefix of its citation token. A calendar event is not
#: in our database, so it is named by what addresses it in the app: the connected account and the
#: event's id (the calendar rides on the chip as data, never in the token: its id carries `@`, `.` and
#: `#`, which a citation token cannot).
EVENT_SOURCE = "calendar_event"

#: What an event id may hold to be written into a token: Google's own alphabet (base32hex) plus the
#: `_` and the time of a recurring instance. Anything else gets no token and no chip.
_TOKEN_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,1024}$")


def event_cite(account_id: Any, event_id: Any) -> str | None:
    """`calendar_event:<account_id>:<event_id>`: the token the model cites an event with, and the
    `entity_id` of its chip. None when the event id cannot be part of one."""
    if not account_id or not isinstance(event_id, str) or not _TOKEN_SAFE.match(event_id):
        return None
    return f"{EVENT_SOURCE}:{account_id}:{event_id}"


def account_fields(account: ConnectedAccount, *, defaulted: bool, label: bool = False) -> dict:
    """What a result says of the account it was made on, while `AGENT_PARITY_TOOLS` is on.

    `account_id` always (the handle the next call passes); `account_label` (the address the member
    knows it by) on the reads, once per call. When the call took the default and the member has more
    accounts, `other_accounts` counts them: a read of "what is on my calendar" covered one account
    and the model has to be able to tell."""
    out: dict[str, Any] = {"account_id": str(account.id)}
    if label:
        out["account_label"] = account.label
    if defaulted:
        others = (
            ConnectedAccount.objects.filter(user_id=account.user_id, provider="google")
            .exclude(id=account.id)
            .count()
        )
        if others:
            out["other_accounts"] = others
    return out


def cite_event(row: dict[str, Any], account_id: Any, ctx: ToolContext) -> dict[str, Any]:
    """`row` (a shaped event) with the token to cite it by, while the flag is on. MCP has no citation
    grammar, so its client is not given one."""
    if ctx.surface == "mcp":
        return row
    token = event_cite(account_id, row.get("id"))
    return {**row, "cite": token} if token else row
