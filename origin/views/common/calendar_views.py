"""Google Calendar pass-through endpoints.

The per-source endpoints all share the same shape:
  - Require auth.
  - Resolve which of the signed-in user's Google ConnectedAccounts to
    act on, from the request's `account_id` (falling back to their
    deterministic default when it's absent).
  - If they have none, 400 with `{detail: "google_not_connected"}` so
    the frontend can render a "Connect Google" prompt.
  - Otherwise call the upstream Calendar v3 endpoint via the token
    helper (which transparently refreshes expiring access tokens).

A user may connect several Google accounts (work and personal), so
`account_id` is part of an event's identity: event ids are unique within
a calendar, not globally, and the account decides which credential the
call is made with. The triple (account_id, calendar_id, event_id)
addresses an event; anything less is ambiguous.

`CalendarEventsAggregateView` is the exception to the shape above — it
spans many accounts and calendars at once and fails per-source rather
than as a whole, so the calendar modal can overlay every selected
calendar and still render when one account's token has died.

We intentionally don't model Calendar events in our DB. Google is the
source of truth; we just present its data.
"""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
from rest_framework import permissions, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from origin.models.common.user_models import ConnectedAccount
from origin.services.oauth.accounts import accounts_for, resolve_account_for
from origin.services.oauth.tokens import ReauthRequired, get_valid_access_token

logger = logging.getLogger(__name__)

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
# Minimum scope needed to read AND write calendar events. A user who
# only signed in with Google ("login" intent) has openid/email/profile
# and will hit Google-side 403s on any Calendar v3 call. Checking up
# front lets us surface a clean, actionable signal to the frontend.
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def _cal(calendar_id: str) -> str:
    """Percent-encode a calendar id for use as a URL path segment.

    Ids are usually email-like and pass through untouched, but Google's
    holiday and some shared calendars carry a literal `#`
    ("en.japanese#holiday@group.v.calendar.google.com"). Interpolated
    raw, that `#` starts a URL fragment and silently truncates the path,
    so the request lands on the wrong endpoint. Unreachable while the UI
    only ever asked for "primary"; reachable now that every subscribed
    calendar is selectable.
    """
    return quote(calendar_id, safe="")


def _has_calendar_scope(account: ConnectedAccount) -> bool:
    return CALENDAR_EVENTS_SCOPE in (account.scopes or [])


def _google_request(account: ConnectedAccount, method: str, path: str, **kwargs):
    """Wrap `requests` with auto-refresh of the access token."""
    token = get_valid_access_token(account)
    return _google_request_with_token(token, method, path, **kwargs)


def _google_request_with_token(token: str, method: str, path: str, **kwargs):
    """Same call, but against an already-resolved token.

    Split out for the aggregate endpoint, which fans out over several
    accounts in a thread pool: `get_valid_access_token` can write the
    refreshed token back to the row, and doing ORM work from pool
    threads means each one checks out its own DB connection (and can
    race two refreshes of the same row). Resolving every token on the
    request thread first keeps the pool purely I/O-bound over HTTP.
    """
    headers = kwargs.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {token}"
    headers.setdefault("Accept", "application/json")
    return requests.request(
        method, f"{CALENDAR_API_BASE}{path}", headers=headers, timeout=15, **kwargs
    )


def _not_connected() -> Response:
    return Response({"detail": "google_not_connected"}, status=status.HTTP_400_BAD_REQUEST)


def _account_not_found() -> Response:
    # 404 rather than 403: the id either isn't a real account or isn't
    # this user's, and the two must be indistinguishable so an account
    # UUID can't be probed for existence.
    return Response({"detail": "calendar_account_not_found"}, status=status.HTTP_404_NOT_FOUND)


def _scope_missing() -> Response:
    # 403 because the account is connected; the user just lacks the
    # specific permission to operate on Calendar. The frontend uses
    # this discriminator to render a "Grant Calendar access" button
    # that re-runs the OAuth flow with the connect-intent scopes
    # (which upgrades the existing account's scopes in place).
    return Response({"detail": "calendar_scope_missing"}, status=status.HTTP_403_FORBIDDEN)


def _reauth_required() -> Response:
    # 400, mirroring `_not_connected`: the account row exists (and may
    # still carry the calendar scope), but its stored Google credential
    # can no longer be refreshed — the refresh token was revoked or
    # expired. Same `detail`-discriminator contract lets the frontend
    # render a "Reconnect Google Calendar" button that re-runs the
    # connect-intent OAuth flow (minting a fresh refresh token). Kept
    # distinct from `google_not_connected` so the UI can say "reconnect"
    # rather than "connect", and from `calendar_scope_missing` so it
    # doesn't mislead the user into thinking a scope was dropped.
    return Response({"detail": "google_reauth_required"}, status=status.HTTP_400_BAD_REQUEST)


def _resolve_account(user, account_id: str | None = None):
    """Centralised gate for every Calendar endpoint. Returns either
    `(account, None)` on success, or `(None, error_response)` so the
    caller can `return error_response`. Pulling the scope check up
    here means a Google sign-in-only user gets the same clean signal
    on every endpoint instead of leaking an upstream 403.

    `account_id` names which of the user's Google accounts to act on.
    Omitting it resolves the deterministic default, which is what keeps
    every pre-multi-account caller (and the agent tools) working
    unchanged.
    """
    account, err = resolve_account_for(user, "google", account_id)
    if err == "not_connected":
        return None, _not_connected()
    if err == "account_not_found":
        return None, _account_not_found()
    if not _has_calendar_scope(account):
        return None, _scope_missing()
    # Warm the access token up front so a dead refresh token surfaces as
    # a clean `google_reauth_required` on every endpoint, rather than as
    # an uncaught 500 from deep inside `_google_request`. This only hits
    # the network when the cached token is near expiry, and it persists
    # the refreshed token on the row — so the later `get_valid_access_token`
    # call in `_google_request` is just a cheap decrypt.
    try:
        get_valid_access_token(account)
    except ReauthRequired:
        return None, _reauth_required()
    return account, None


class CalendarListView(APIView):
    """GET /api/v2/calendar/list/ — every calendar the user can see,
    across every Google account they've connected.

    Each entry is tagged with the account it came from, because
    calendar ids are only unique *within* an account and the client
    needs the (account_id, calendar_id) pair to fetch or write events.

    Calendars other people have shared with the user come back here for
    free: Google's `calendarList` is the user's subscription list, not
    just the calendars they own, so a teammate's shared calendar is
    already in the response with `access_role` saying what they may do
    with it.

    One dead or unscoped account never blanks the whole picker — its
    failure is reported in `failed_accounts` and the calendars from the
    healthy accounts are still returned.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request):
        accounts = list(accounts_for(request.user, "google"))
        if not accounts:
            return _not_connected()

        calendars: list[dict] = []
        failed: list[dict] = []
        for account in accounts:
            if not _has_calendar_scope(account):
                failed.append({"account_id": str(account.id), "reason": "calendar_scope_missing"})
                continue
            try:
                resp = _google_request(account, "GET", "/users/me/calendarList")
            except ReauthRequired:
                failed.append({"account_id": str(account.id), "reason": "google_reauth_required"})
                continue
            except requests.RequestException as exc:
                logger.warning("Calendar list transport error account=%s: %s", account.id, exc)
                failed.append({"account_id": str(account.id), "reason": "calendar_api_error"})
                continue
            if not resp.ok:
                logger.warning(
                    "Calendar list failed account=%s: %s %s",
                    account.id,
                    resp.status_code,
                    resp.text[:500],
                )
                failed.append({"account_id": str(account.id), "reason": "calendar_api_error"})
                continue
            for c in resp.json().get("items", []):
                calendars.append(
                    {
                        "id": c["id"],
                        "account_id": str(account.id),
                        "account_email": account.provider_email,
                        "summary": c.get("summary"),
                        "primary": bool(c.get("primary", False)),
                        "background_color": c.get("backgroundColor"),
                        # "reader" for a calendar someone shared read-only;
                        # the client greys out create/edit for those rather
                        # than letting the user write and take a 403.
                        "access_role": c.get("accessRole"),
                        "selected": bool(c.get("selected", False)),
                    }
                )

        # Every account failed and none produced calendars — surface the
        # first reason as the endpoint-level error so a single-account
        # user gets exactly the discriminator they got before this
        # endpoint became multi-account.
        if not calendars and failed:
            reason = failed[0]["reason"]
            if reason == "calendar_scope_missing":
                return _scope_missing()
            if reason == "google_reauth_required":
                return _reauth_required()
            return Response({"detail": "calendar_api_error"}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(
            {
                "calendars": calendars,
                "failed_accounts": failed,
                "accounts": [
                    {
                        "id": str(a.id),
                        "email": a.provider_email,
                        "label": a.label,
                        "is_primary": a.is_login_identity,
                    }
                    for a in accounts
                ],
            }
        )


#: Upper bound on the fan-out. A user with two accounts × a dozen
#: subscribed calendars each would otherwise open 24 sockets per paint.
#: Sources beyond the limit are reported as failed rather than silently
#: dropped, so the client can tell the user to narrow their selection.
MAX_AGGREGATE_SOURCES = 20
#: Threads for the fan-out. These are pure network waits, so the pool
#: only needs to be wide enough to overlap the latency, not to match
#: source count.
AGGREGATE_POOL_SIZE = 8


def _parse_sources(raw: str) -> list[tuple[str, str]]:
    """Parse `sources=<account_id>:<calendar_id>,...` into pairs.

    Calendar ids are email-like (`en.japanese#holiday@group.v.calendar.google.com`)
    and contain no comma, but they *can* contain a colon, so the split
    is on the first colon only. Malformed entries are skipped rather
    than 400-ing the whole request: one bad saved selection shouldn't
    stop the other calendars from rendering.
    """
    out: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        account_id, calendar_id = chunk.split(":", 1)
        account_id, calendar_id = account_id.strip(), calendar_id.strip()
        if account_id and calendar_id:
            out.append((account_id, calendar_id))
    return out


class CalendarEventsAggregateView(APIView):
    """GET /api/v2/calendar/events/aggregate/ — events from many
    (account, calendar) pairs merged into one response.

    This exists so the calendar modal can overlay every account and
    calendar the user has selected in a single view. Two properties the
    per-source endpoint can't provide:

      1. **Partial success.** A revoked refresh token on the user's
         personal account must not blank out their work calendar. Each
         source fails independently into `failed_sources`; whatever
         loaded still comes back in `items`.
      2. **One round trip.** The client would otherwise fan out N
         requests per paint and have to reconcile N loading states.

    Every event is tagged with a `_source` block naming the account and
    calendar it came from. That triple — (account_id, calendar_id,
    event_id) — is the only way to identify an event for a later edit or
    delete: event ids are unique per calendar, not globally, and the
    client has no other way to know which account to authenticate the
    PATCH against.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request):
        accounts = {str(a.id): a for a in accounts_for(request.user, "google")}
        if not accounts:
            return _not_connected()

        raw_sources = (request.GET.get("sources") or "").strip()
        if raw_sources:
            requested = _parse_sources(raw_sources)
        else:
            # No explicit selection: every connected account's primary
            # calendar. Chosen so a client that hasn't loaded the
            # calendar list yet still renders something sensible, and
            # without spending a `calendarList` call per account.
            requested = [(aid, "primary") for aid in accounts]

        failed: list[dict] = []
        # Resolve tokens on the request thread — see
        # `_google_request_with_token` for why the pool must not touch
        # the ORM.
        runnable: list[tuple[str, str, str]] = []
        token_cache: dict[str, str | None] = {}
        for account_id, calendar_id in requested[:MAX_AGGREGATE_SOURCES]:
            account = accounts.get(account_id)
            if account is None:
                failed.append(
                    {
                        "account_id": account_id,
                        "calendar_id": calendar_id,
                        "reason": "calendar_account_not_found",
                    }
                )
                continue
            if not _has_calendar_scope(account):
                failed.append(
                    {
                        "account_id": account_id,
                        "calendar_id": calendar_id,
                        "reason": "calendar_scope_missing",
                    }
                )
                continue
            if account_id not in token_cache:
                # Cached per account, not per source: a user with eight
                # calendars on one account should decrypt (and possibly
                # refresh) that account's token once, not eight times.
                try:
                    token_cache[account_id] = get_valid_access_token(account)
                except ReauthRequired:
                    token_cache[account_id] = None
            token = token_cache[account_id]
            if token is None:
                failed.append(
                    {
                        "account_id": account_id,
                        "calendar_id": calendar_id,
                        "reason": "google_reauth_required",
                    }
                )
                continue
            runnable.append((account_id, calendar_id, token))

        for account_id, calendar_id in requested[MAX_AGGREGATE_SOURCES:]:
            failed.append(
                {
                    "account_id": account_id,
                    "calendar_id": calendar_id,
                    "reason": "too_many_sources",
                }
            )

        params = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": request.GET.get("max_results", "250"),
        }
        for src, dst in (("from", "timeMin"), ("to", "timeMax")):
            v = request.GET.get(src)
            if v:
                params[dst] = v

        items: list[dict] = []
        if runnable:
            with ThreadPoolExecutor(max_workers=min(AGGREGATE_POOL_SIZE, len(runnable))) as pool:
                futures = {
                    pool.submit(self._fetch_source, token, account_id, calendar_id, params): (
                        account_id,
                        calendar_id,
                    )
                    for account_id, calendar_id, token in runnable
                }
                for future in as_completed(futures):
                    account_id, calendar_id = futures[future]
                    try:
                        fetched = future.result()
                    except requests.RequestException as exc:
                        logger.warning(
                            "Aggregate fetch transport error account=%s calendar=%s: %s",
                            account_id,
                            calendar_id,
                            exc,
                        )
                        fetched = None
                    if fetched is None:
                        failed.append(
                            {
                                "account_id": account_id,
                                "calendar_id": calendar_id,
                                "reason": "calendar_api_error",
                            }
                        )
                        continue
                    account = accounts[account_id]
                    for event in fetched:
                        event["_source"] = {
                            "account_id": account_id,
                            "account_email": account.provider_email,
                            "calendar_id": calendar_id,
                        }
                        items.append(event)

        # Sorted server-side so the client can render straight from the
        # array. The pool completes out of order, and each source is
        # only internally sorted, so a merge without this would
        # interleave calendars arbitrarily. Events with no parseable
        # start sort last rather than blowing up the comparison.
        items.sort(key=_event_sort_key)
        return Response({"items": items, "failed_sources": failed})

    @staticmethod
    def _fetch_source(
        token: str, account_id: str, calendar_id: str, params: dict
    ) -> list[dict] | None:
        """Fetch one calendar. Returns None on a non-OK upstream reply so
        the caller records it as a failed source. Runs on a pool thread:
        no ORM access, no shared mutable state."""
        resp = _google_request_with_token(
            token,
            "GET",
            f"/calendars/{_cal(calendar_id)}/events",
            params=params,
        )
        if not resp.ok:
            logger.warning(
                "Aggregate fetch failed account=%s calendar=%s status=%s body=%s",
                account_id,
                calendar_id,
                resp.status_code,
                resp.text[:300],
            )
            return None
        return resp.json().get("items", []) or []


def _event_sort_key(event: dict) -> tuple[int, str]:
    """Order merged events by start time. All-day events carry `date`
    ("YYYY-MM-DD") and timed ones `dateTime` (full ISO); comparing the
    raw strings sorts an all-day event before every timed event that
    day, which is the conventional rendering order. The leading int
    pushes events with no usable start to the end of the list."""
    start = event.get("start") or {}
    raw = start.get("date") or start.get("dateTime")
    return (1, "") if not isinstance(raw, str) else (0, raw)


class CalendarEventsView(APIView):
    """GET  /api/v2/calendar/events/ — list events in a date range.
    POST /api/v2/calendar/events/ — create an event."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request):
        account, _err = _resolve_account(request.user, request.GET.get("account_id"))
        if _err is not None:
            return _err
        calendar_id = request.GET.get("calendar_id", "primary")
        params = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": request.GET.get("max_results", "50"),
        }
        for src, dst in (("from", "timeMin"), ("to", "timeMax")):
            v = request.GET.get(src)
            if v:
                params[dst] = v
        resp = _google_request(
            account, "GET", f"/calendars/{_cal(calendar_id)}/events", params=params
        )
        if not resp.ok:
            logger.warning("Calendar events GET failed: %s %s", resp.status_code, resp.text)
            return Response(
                {"detail": "calendar_api_error", "upstream_status": resp.status_code},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(resp.json())

    def post(self, request: Request):
        account, _err = _resolve_account(request.user, request.data.get("account_id"))
        if _err is not None:
            return _err
        calendar_id = request.data.get("calendar_id", "primary")
        body = {
            "summary": request.data.get("summary"),
            "description": request.data.get("description"),
            "start": request.data.get("start"),
            "end": request.data.get("end"),
        }
        # Drop Nones so Google doesn't choke on null fields.
        body = {k: v for k, v in body.items() if v is not None}
        if "summary" not in body or "start" not in body or "end" not in body:
            return Response(
                {"detail": "summary, start, and end are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Attendees: when provided, copy through to Google so the
        # event shows up on each attendee's calendar. We suppress
        # email notifications (`sendUpdates=none`) because the
        # callers driving this today (Quick Meet) post the link
        # into chat — emailing on top of that is duplicate noise.
        attendees = request.data.get("attendees")
        params = {}
        if isinstance(attendees, list) and attendees:
            # Defensively rebuild the list so a malformed payload
            # can't smuggle arbitrary Calendar event keys.
            cleaned = []
            for a in attendees:
                if not isinstance(a, dict):
                    continue
                email = a.get("email")
                if not email or not isinstance(email, str):
                    continue
                entry = {"email": email}
                display_name = a.get("displayName")
                if isinstance(display_name, str) and display_name:
                    entry["displayName"] = display_name
                cleaned.append(entry)
            if cleaned:
                body["attendees"] = cleaned
                params["sendUpdates"] = "none"
        # When the caller asks for a Google Meet link, attach a
        # createRequest. Google may return the link inline OR with
        # status.statusCode="pending" — clients must handle both.
        # `conferenceDataVersion=1` is required for Google to honor the
        # createRequest at all; without it, the field is silently
        # dropped.
        if request.data.get("add_meet"):
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": uuid.uuid4().hex,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
            params["conferenceDataVersion"] = 1
        resp = _google_request(
            account,
            "POST",
            f"/calendars/{_cal(calendar_id)}/events",
            json=body,
            params=params or None,
        )
        if not resp.ok:
            logger.warning("Calendar event create failed: %s %s", resp.status_code, resp.text)
            return Response(
                {
                    "detail": "calendar_api_error",
                    "upstream_status": resp.status_code,
                    "upstream_body": resp.json() if resp.text else None,
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(resp.json(), status=status.HTTP_201_CREATED)


class CalendarEventDetailView(APIView):
    """GET    /api/v2/calendar/events/<event_id>/ — fetch one event.
    PATCH  /api/v2/calendar/events/<event_id>/ — update an event.
    DELETE /api/v2/calendar/events/<event_id>/ — delete an event."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request, event_id: str):
        account, _err = _resolve_account(request.user, request.GET.get("account_id"))
        if _err is not None:
            return _err
        calendar_id = request.GET.get("calendar_id", "primary")
        resp = _google_request(account, "GET", f"/calendars/{_cal(calendar_id)}/events/{event_id}")
        if resp.status_code == 404:
            # Event was deleted upstream (or the ID never existed). The
            # frontend surfaces this as a distinct state so the user can
            # unlink without it looking like a transient network error.
            return Response(
                {"detail": "event_deleted_upstream"},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not resp.ok:
            logger.warning("Calendar event GET failed: %s %s", resp.status_code, resp.text)
            return Response(
                {"detail": "calendar_api_error", "upstream_status": resp.status_code},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(resp.json())

    def patch(self, request: Request, event_id: str):
        account, _err = _resolve_account(request.user, request.data.get("account_id"))
        if _err is not None:
            return _err
        calendar_id = request.data.get("calendar_id", "primary")
        body = {
            k: v
            for k, v in {
                "summary": request.data.get("summary"),
                "description": request.data.get("description"),
                "start": request.data.get("start"),
                "end": request.data.get("end"),
            }.items()
            if v is not None
        }
        # Attendees on PATCH: same shape as POST. When provided,
        # Google's PATCH overwrites the entire attendees list, so
        # the caller must send the full intended list (not a diff).
        # Omitting attendees leaves the existing list untouched on
        # Google's side, which is what users want when they edit a
        # non-attendee field.
        attendees = request.data.get("attendees")
        params = {}
        if isinstance(attendees, list):
            cleaned = []
            for a in attendees:
                if not isinstance(a, dict):
                    continue
                email = a.get("email")
                if not email or not isinstance(email, str):
                    continue
                entry = {"email": email}
                display_name = a.get("displayName")
                if isinstance(display_name, str) and display_name:
                    entry["displayName"] = display_name
                cleaned.append(entry)
            body["attendees"] = cleaned
            # Suppress email invites for the same reason as POST —
            # the chat / event modal is the notification surface.
            if cleaned:
                params["sendUpdates"] = "none"
        # Meet toggle semantics (only when `add_meet` is explicitly in
        # the request — omitting it leaves the event's Meet state
        # untouched):
        #   add_meet=true  → attach a createRequest. If the event
        #                    already has a Meet link, Google preserves
        #                    it; otherwise it generates one.
        #   add_meet=false → null out `conferenceData`, which removes
        #                    any existing Meet link.
        # The createRequest path needs `conferenceDataVersion=1`;
        # without it Google silently drops the field.
        if "add_meet" in request.data:
            if request.data.get("add_meet"):
                body["conferenceData"] = {
                    "createRequest": {
                        "requestId": uuid.uuid4().hex,
                        "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    }
                }
            else:
                body["conferenceData"] = None
            params["conferenceDataVersion"] = 1
        resp = _google_request(
            account,
            "PATCH",
            f"/calendars/{_cal(calendar_id)}/events/{event_id}",
            json=body,
            params=params or None,
        )
        if not resp.ok:
            logger.warning("Calendar event patch failed: %s %s", resp.status_code, resp.text)
            return Response(
                {"detail": "calendar_api_error", "upstream_status": resp.status_code},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(resp.json())

    def delete(self, request: Request, event_id: str):
        account, _err = _resolve_account(request.user, request.GET.get("account_id"))
        if _err is not None:
            return _err
        calendar_id = request.GET.get("calendar_id", "primary")
        resp = _google_request(
            account, "DELETE", f"/calendars/{_cal(calendar_id)}/events/{event_id}"
        )
        if not resp.ok and resp.status_code != 404:
            logger.warning("Calendar event delete failed: %s %s", resp.status_code, resp.text)
            return Response(
                {"detail": "calendar_api_error", "upstream_status": resp.status_code},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
