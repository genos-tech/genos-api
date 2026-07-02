"""Google Calendar pass-through endpoints.

All five endpoints share the same shape:
  - Require auth.
  - Look up the signed-in user's Google ConnectedAccount.
  - If missing, 400 with `{detail: "google_not_connected"}` so the
    frontend can render a "Connect Google" prompt.
  - Otherwise call the upstream Calendar v3 endpoint via the token
    helper (which transparently refreshes expiring access tokens).

We intentionally don't model Calendar events in our DB. Google is the
source of truth; we just present its data.
"""

from __future__ import annotations

import logging
import uuid

import requests
from rest_framework import permissions, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from origin.models.common.user_models import ConnectedAccount
from origin.services.oauth.tokens import ReauthRequired, get_valid_access_token

logger = logging.getLogger(__name__)

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
# Minimum scope needed to read AND write calendar events. A user who
# only signed in with Google ("login" intent) has openid/email/profile
# and will hit Google-side 403s on any Calendar v3 call. Checking up
# front lets us surface a clean, actionable signal to the frontend.
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def _connected_account(user) -> ConnectedAccount | None:
    return ConnectedAccount.objects.filter(user=user, provider="google").first()


def _has_calendar_scope(account: ConnectedAccount) -> bool:
    return CALENDAR_EVENTS_SCOPE in (account.scopes or [])


def _google_request(account: ConnectedAccount, method: str, path: str, **kwargs):
    """Wrap `requests` with auto-refresh of the access token."""
    token = get_valid_access_token(account)
    headers = kwargs.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {token}"
    headers.setdefault("Accept", "application/json")
    return requests.request(
        method, f"{CALENDAR_API_BASE}{path}", headers=headers, timeout=15, **kwargs
    )


def _not_connected() -> Response:
    return Response({"detail": "google_not_connected"}, status=status.HTTP_400_BAD_REQUEST)


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


def _resolve_account(user) -> tuple[ConnectedAccount | None, Response | None]:
    """Centralised gate for every Calendar endpoint. Returns either
    `(account, None)` on success, or `(None, error_response)` so the
    caller can `return error_response`. Pulling the scope check up
    here means a Google sign-in-only user gets the same clean signal
    on every endpoint instead of leaking an upstream 403."""
    account = _connected_account(user)
    if account is None:
        return None, _not_connected()
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
    """GET /api/v2/calendar/list/ — return the user's calendars so the
    UI can show a picker."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request):
        account, _err = _resolve_account(request.user)
        if _err is not None:
            return _err
        resp = _google_request(account, "GET", "/users/me/calendarList")
        if not resp.ok:
            logger.warning("Calendar list failed: %s %s", resp.status_code, resp.text)
            return Response(
                {"detail": "calendar_api_error", "upstream_status": resp.status_code},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        data = resp.json()
        return Response(
            {
                "calendars": [
                    {
                        "id": c["id"],
                        "summary": c.get("summary"),
                        "primary": bool(c.get("primary", False)),
                        "background_color": c.get("backgroundColor"),
                    }
                    for c in data.get("items", [])
                ]
            }
        )


class CalendarEventsView(APIView):
    """GET  /api/v2/calendar/events/ — list events in a date range.
    POST /api/v2/calendar/events/ — create an event."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request):
        account, _err = _resolve_account(request.user)
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
        resp = _google_request(account, "GET", f"/calendars/{calendar_id}/events", params=params)
        if not resp.ok:
            logger.warning("Calendar events GET failed: %s %s", resp.status_code, resp.text)
            return Response(
                {"detail": "calendar_api_error", "upstream_status": resp.status_code},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(resp.json())

    def post(self, request: Request):
        account, _err = _resolve_account(request.user)
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
            account, "POST", f"/calendars/{calendar_id}/events", json=body, params=params or None
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
        account, _err = _resolve_account(request.user)
        if _err is not None:
            return _err
        calendar_id = request.GET.get("calendar_id", "primary")
        resp = _google_request(account, "GET", f"/calendars/{calendar_id}/events/{event_id}")
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
        account, _err = _resolve_account(request.user)
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
            f"/calendars/{calendar_id}/events/{event_id}",
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
        account, _err = _resolve_account(request.user)
        if _err is not None:
            return _err
        calendar_id = request.GET.get("calendar_id", "primary")
        resp = _google_request(account, "DELETE", f"/calendars/{calendar_id}/events/{event_id}")
        if not resp.ok and resp.status_code != 404:
            logger.warning("Calendar event delete failed: %s %s", resp.status_code, resp.text)
            return Response(
                {"detail": "calendar_api_error", "upstream_status": resp.status_code},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
