"""Google OAuth 2.0 provider.

Used for two intents that share the same flow shape but differ in
scopes:
  - login:   `openid email profile` — just identifies the user.
  - connect: adds Calendar scopes so the access token can list /
             create / update / delete events on the user's calendars.

We talk to Google's REST endpoints directly via `requests` rather than
pulling in `google-api-python-client` — the calendar API surface we
need is small (5 endpoints), the OAuth flow is straightforward, and
the library is heavy.
"""

from urllib.parse import urlencode

import requests
from django.conf import settings

from .base import FlowIntent, OAuthProvider, ProviderProfile, TokenResponse

# Scopes by intent. `login` is the minimum needed to identify a user;
# `connect` is what powers the Calendar feature. Adding new Google
# features later means broadening this list in one place.
GOOGLE_LOGIN_SCOPES = ["openid", "email", "profile"]
GOOGLE_CONNECT_SCOPES = GOOGLE_LOGIN_SCOPES + [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleOAuthProvider(OAuthProvider):
    name = "google"

    def authorize_url(self, *, state: str, intent: FlowIntent, redirect_uri: str) -> str:
        scopes = GOOGLE_CONNECT_SCOPES if intent == "connect" else GOOGLE_LOGIN_SCOPES
        params: dict[str, str] = {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "include_granted_scopes": "true",
        }
        # Both flows show the account chooser, because both are
        # meaningless without it once the browser already holds a Google
        # session: Google would otherwise resolve the flow against that
        # remembered account silently. On `connect` that turns "Add
        # account" into a no-op — the callback gets the same `sub` back,
        # finds the existing row and refreshes it. On `login` it decides
        # WHO is signing in without asking, so a user who meant a
        # different account has no way to say so, and any refusal reads
        # as nonsense: they were never shown an address to begin with.
        #
        # `access_type=offline` and the extra `consent` are `connect`-only.
        # That flow needs a refresh_token to call the Calendar API later
        # and Google only issues one on a fresh consent. `login` just
        # identifies the user — we mint our own JWT and never touch the
        # Google token again — so forcing a re-consent there would only
        # add a screen. `prompt` takes a space-delimited list.
        if intent == "connect":
            params["access_type"] = "offline"
            params["prompt"] = "select_account consent"
        else:
            params["prompt"] = "select_account"
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code(self, *, code: str, redirect_uri: str) -> TokenResponse:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return TokenResponse(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in_seconds=data.get("expires_in"),
            granted_scopes=(data.get("scope") or "").split() or [],
        )

    def refresh(self, *, refresh_token: str) -> TokenResponse:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # Google omits refresh_token on refresh responses; the existing
        # one stays valid. Callers should keep the old refresh_token
        # if the new TokenResponse doesn't carry one.
        return TokenResponse(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in_seconds=data.get("expires_in"),
            granted_scopes=(data.get("scope") or "").split() or [],
        )

    def fetch_profile(self, *, access_token: str) -> ProviderProfile:
        resp = requests.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return ProviderProfile(
            provider_user_id=data["sub"],
            email=data.get("email"),
            display_name=data.get("name") or data.get("given_name"),
        )
