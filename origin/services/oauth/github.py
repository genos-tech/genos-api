"""GitHub OAuth provider.

Login asks for the minimum scopes needed to identify the user; connect
adds `repo` because GitHub OAuth Apps don't offer a finer-grained
"read-only PR" scope. Read-only behaviour is enforced by application
discipline — every outbound call in `github_views.py` is a GET.

Tokens issued by GitHub OAuth Apps don't expire by default, so
`supports_refresh` is False and `refresh()` raises.
"""

from urllib.parse import urlencode

import requests
from django.conf import settings

from .base import FlowIntent, OAuthProvider, ProviderProfile, TokenResponse

GITHUB_LOGIN_SCOPES = ["read:user", "user:email"]
GITHUB_CONNECT_SCOPES = GITHUB_LOGIN_SCOPES + ["repo"]

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
USER_EMAILS_URL = "https://api.github.com/user/emails"


class GitHubOAuthProvider(OAuthProvider):
    name = "github"

    @property
    def supports_refresh(self) -> bool:
        return False

    def authorize_url(self, *, state: str, intent: FlowIntent, redirect_uri: str) -> str:
        scopes = GITHUB_CONNECT_SCOPES if intent == "connect" else GITHUB_LOGIN_SCOPES
        params = {
            "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "allow_signup": "true",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code(self, *, code: str, redirect_uri: str) -> TokenResponse:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
                "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            # GitHub returns form-encoded by default; ask for JSON.
            headers={"Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            # GitHub returns 200 with {"error": "..."} for some failures
            # (e.g. bad code). Surface that as an exception rather than
            # KeyError later.
            raise RuntimeError(f"GitHub OAuth exchange failed: {data}")
        return TokenResponse(
            access_token=data["access_token"],
            refresh_token=None,  # OAuth Apps don't issue refresh tokens.
            expires_in_seconds=None,  # No expiry by default.
            granted_scopes=(data.get("scope") or "").split(",") if data.get("scope") else [],
        )

    def refresh(self, *, refresh_token: str) -> TokenResponse:
        raise NotImplementedError(
            "GitHub OAuth App tokens don't expire and don't support refresh."
        )

    def fetch_profile(self, *, access_token: str) -> ProviderProfile:
        # Primary identity comes from /user. The `email` field there is
        # None for users with private email — fall back to /user/emails
        # which the `user:email` scope unlocks.
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        u = requests.get(USER_URL, headers=headers, timeout=10)
        u.raise_for_status()
        user = u.json()
        email = user.get("email")
        if not email:
            try:
                e = requests.get(USER_EMAILS_URL, headers=headers, timeout=10)
                e.raise_for_status()
                emails = e.json()
                primary = next((x for x in emails if x.get("primary") and x.get("verified")), None)
                if primary:
                    email = primary["email"]
            except requests.RequestException:
                # Best-effort; we can still create the user without an
                # email if GitHub won't share one.
                pass
        return ProviderProfile(
            provider_user_id=str(user["id"]),
            email=email,
            display_name=user.get("name") or user.get("login"),
        )
