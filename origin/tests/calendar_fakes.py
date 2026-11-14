"""A fake Google Calendar for the tests of the calendar tools (MIP-1059). No network, ever.

Google is reached through ONE seam: `requests.request`, called by `agent.calendar.google_calendar_request`
(the agent's tools) and by `views.common.calendar_views._google_request_with_token` (the app's endpoints).
`FakeGoogle.patched()` replaces that function and the two token helpers, so a test can say which
ACCOUNT a call was made with (each account's token is `token-<provider_user_id>`), what path, query and body
it sent, and what a second account would have seen instead.

Events live in `FakeGoogle.events`, keyed `(token, calendar id, event id)`: ids are unique per
calendar and the account decides which credential the call is made with, exactly as in the app.
"""

from __future__ import annotations

import copy
import itertools
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest import mock
from urllib.parse import unquote

import requests

from origin.models.common.user_models import ConnectedAccount

CALENDAR_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]
V3 = "https://www.googleapis.com/calendar/v3"


@dataclass
class Call:
    method: str
    url: str
    path: str  # decoded, without the API base
    params: dict[str, Any]
    body: Any
    token: str
    timeout: Any = None

    @property
    def raw_path(self) -> str:
        return self.url[len(V3) :]


def token_for(account: ConnectedAccount) -> str:
    return f"token-{account.provider_user_id}"


def response(payload: Any = None, status: int = 200) -> mock.MagicMock:
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.text = ""
    resp.headers = {}
    resp.json.return_value = copy.deepcopy(payload) if payload is not None else {}
    return resp


@dataclass
class FakeGoogle:
    #: `(token, calendar id, event id) -> the event Google would return`
    events: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    #: `token -> the calendarList items of that account`
    calendars: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: tokens whose every call answers `status`
    failing: dict[str, int] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    # ---- what a test sets up ---------------------------------------------------------------

    def add_event(
        self, account: ConnectedAccount, calendar_id: str, event_id: str, **fields: Any
    ) -> dict[str, Any]:
        event = {
            "id": event_id,
            "summary": fields.pop("summary", f"Event {event_id}"),
            "start": fields.pop("start", {"dateTime": "2026-12-01T10:00:00+09:00"}),
            "end": fields.pop("end", {"dateTime": "2026-12-01T11:00:00+09:00"}),
            "status": "confirmed",
            **fields,
        }
        self.events[(token_for(account), calendar_id, event_id)] = event
        return event

    def add_calendar(self, account: ConnectedAccount, calendar_id: str, **fields: Any) -> None:
        self.calendars.setdefault(token_for(account), []).append(
            {"id": calendar_id, "summary": calendar_id, **fields}
        )

    # ---- what the code under test sees ------------------------------------------------------

    def of(self, method: str | None = None) -> list[Call]:
        return [c for c in self.calls if method is None or c.method == method]

    def request(self, method: str, url: str, **kwargs: Any) -> mock.MagicMock:
        assert url.startswith(V3), f"a call that is not Calendar v3: {url}"
        headers = kwargs.get("headers") or {}
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        path = unquote(url[len(V3) :])
        call = Call(
            method=method,
            url=url,
            path=path,
            params=dict(kwargs.get("params") or {}),
            body=copy.deepcopy(kwargs.get("json")),
            token=token,
            timeout=kwargs.get("timeout"),
        )
        self.calls.append(call)
        if token in self.failing:
            return response({}, self.failing[token])
        if path == "/users/me/calendarList":
            return response({"items": self.calendars.get(token, [])})
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "calendars" and parts[2] == "events":
            calendar_id = parts[1]
            if len(parts) == 3:
                return self._collection(method, token, calendar_id, call)
            return self._member(method, token, calendar_id, "/".join(parts[3:]), call)
        return response({}, 404)

    def _collection(self, method: str, token: str, calendar_id: str, call: Call) -> mock.MagicMock:
        if method == "GET":
            items = [
                e
                for (tok, cal, _eid), e in sorted(self.events.items())
                if tok == token and cal == calendar_id
            ]
            return response({"items": items})
        if method == "POST":
            event = {"id": f"new{next(self._ids)}", "status": "confirmed", **(call.body or {})}
            self.events[(token, calendar_id, event["id"])] = event
            return response(event)
        return response({}, 405)

    def _member(
        self, method: str, token: str, calendar_id: str, event_id: str, call: Call
    ) -> mock.MagicMock:
        key = (token, calendar_id, event_id)
        event = self.events.get(key)
        if event is None:
            return response({}, 404)
        if method == "GET":
            return response(event)
        if method == "PATCH":
            for field_name, value in (call.body or {}).items():
                if value is None:
                    event.pop(field_name, None)
                else:
                    event[field_name] = value
            return response(event)
        if method == "DELETE":
            del self.events[key]
            return response({}, 204)
        return response({}, 405)

    # ---- the seam ----------------------------------------------------------------------------

    @contextmanager
    def patched(self):
        """Google is this fake, and every account's token is `token-<provider_user_id>`."""

        def access_token(account: ConnectedAccount) -> str:
            return token_for(account)

        with ExitStack() as stack:
            stack.enter_context(mock.patch("requests.request", side_effect=self.request))
            for module in (
                "origin.search_engine.agent.calendar",
                "origin.views.common.calendar_views",
            ):
                stack.enter_context(
                    mock.patch(f"{module}.get_valid_access_token", side_effect=access_token)
                )
            yield self


def connect_google(
    user, sub: str, email: str, *, login: bool = False, scopes=None
) -> ConnectedAccount:
    """A Google account of `user`'s, with Calendar access unless `scopes` says otherwise."""
    return ConnectedAccount.objects.create(
        user=user,
        provider="google",
        provider_user_id=sub,
        provider_email=email,
        scopes=CALENDAR_SCOPES if scopes is None else scopes,
        access_token_encrypted="placeholder",
        is_login_identity=login,
    )
