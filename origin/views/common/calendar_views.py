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

import requests
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.request import Request
from rest_framework.views import APIView

from origin.models.common.user_models import ConnectedAccount
from origin.services.oauth.tokens import get_valid_access_token

logger = logging.getLogger(__name__)

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"


def _connected_account(user) -> ConnectedAccount | None:
    return ConnectedAccount.objects.filter(user=user, provider="google").first()


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


class CalendarListView(APIView):
    """GET /api/v2/calendar/list/ — return the user's calendars so the
    UI can show a picker."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request):
        account = _connected_account(request.user)
        if account is None:
            return _not_connected()
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
        account = _connected_account(request.user)
        if account is None:
            return _not_connected()
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
        account = _connected_account(request.user)
        if account is None:
            return _not_connected()
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
        resp = _google_request(account, "POST", f"/calendars/{calendar_id}/events", json=body)
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
    """PATCH  /api/v2/calendar/events/<event_id>/ — update an event.
    DELETE /api/v2/calendar/events/<event_id>/ — delete an event."""

    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request: Request, event_id: str):
        account = _connected_account(request.user)
        if account is None:
            return _not_connected()
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
        resp = _google_request(
            account, "PATCH", f"/calendars/{calendar_id}/events/{event_id}", json=body
        )
        if not resp.ok:
            logger.warning("Calendar event patch failed: %s %s", resp.status_code, resp.text)
            return Response(
                {"detail": "calendar_api_error", "upstream_status": resp.status_code},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(resp.json())

    def delete(self, request: Request, event_id: str):
        account = _connected_account(request.user)
        if account is None:
            return _not_connected()
        calendar_id = request.GET.get("calendar_id", "primary")
        resp = _google_request(account, "DELETE", f"/calendars/{calendar_id}/events/{event_id}")
        if not resp.ok and resp.status_code != 404:
            logger.warning("Calendar event delete failed: %s %s", resp.status_code, resp.text)
            return Response(
                {"detail": "calendar_api_error", "upstream_status": resp.status_code},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
