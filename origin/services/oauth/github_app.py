"""GitHub App user-to-server OAuth provider (`github_app`, MIP-2086).

The App's user token: 8 hours, with a refresh token (6 months) that GitHub ROTATES on every use,
so `rotates_refresh_token` is True and `tokens.get_valid_access_token` refreshes under a row lock.
It reaches only repositories where the App is installed AND the person has access, and carries no
OAuth scopes: the permissions are the App's own (read-only, set at registration).

Not a sign-in provider: the OAuth App (`github.py`) keeps that, so the sign-in identity row is
never touched. `has_flow` is False until the install / authorize flow lands (PR 2); until then
nothing can start a connect with this provider and the registry does not expose it to the views.
"""

from urllib.parse import urlencode

import requests
from django.conf import settings

from .base import FlowIntent, InvalidGrantError, OAuthProvider, ProviderProfile, TokenResponse

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"

# GitHub answers a bad / reused / expired refresh token with HTTP 200 and one of these `error`
# values (the standard `invalid_grant` is accepted too).
_DEAD_REFRESH_ERRORS = {"bad_refresh_token", "invalid_grant"}


class GitHubAppOAuthProvider(OAuthProvider):
    name = "github_app"
    has_flow = False
    rotates_refresh_token = True

    def authorize_url(self, *, state: str, intent: FlowIntent, redirect_uri: str) -> str:
        # GitHub Apps take no `scope` parameter: the permissions are the App's.
        params = {
            "client_id": settings.GITHUB_APP_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code(self, *, code: str, redirect_uri: str) -> TokenResponse:
        return self._token_request(
            {
                "client_id": settings.GITHUB_APP_CLIENT_ID,
                "client_secret": settings.GITHUB_APP_CLIENT_SECRET,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            action="exchange",
        )

    def refresh(self, *, refresh_token: str) -> TokenResponse:
        return self._token_request(
            {
                "client_id": settings.GITHUB_APP_CLIENT_ID,
                "client_secret": settings.GITHUB_APP_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            action="refresh",
        )

    def fetch_profile(self, *, access_token: str) -> ProviderProfile:
        resp = requests.get(
            USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        user = resp.json()
        # A GitHub App user token cannot read `/user/emails`, and this connection is never a
        # sign-in, so no address is claimed: `email_verified` stays False (fail closed, MIP-1766).
        return ProviderProfile(
            provider_user_id=str(user["id"]),
            email=None,
            display_name=user.get("name") or user.get("login"),
        )

    @staticmethod
    def _token_request(data: dict, *, action: str) -> TokenResponse:
        resp = requests.post(
            TOKEN_URL,
            data=data,
            # GitHub returns form-encoded by default; ask for JSON.
            headers={"Accept": "application/json"},
            timeout=(3.05, 10),  # connect, read: bounds the refresh row lock
        )
        resp.raise_for_status()
        body = resp.json()
        if "access_token" not in body:
            error = body.get("error")
            # GitHub's error code only, never the whole answer (it can echo the request).
            if action == "refresh" and error in _DEAD_REFRESH_ERRORS:
                raise InvalidGrantError(f"GitHub App refresh rejected: {error}")
            raise RuntimeError(f"GitHub App token {action} failed: {error}")
        expires_in = body.get("expires_in")
        return TokenResponse(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in_seconds=int(expires_in) if expires_in is not None else None,
            # No OAuth scopes on an App token.
            granted_scopes=[],
        )
