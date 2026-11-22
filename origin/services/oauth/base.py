"""OAuth provider abstraction.

`OAuthProvider` is the contract every concrete provider (Google,
GitHub) implements. The flow code in `oauth_views.py` works against
this interface only — never against a specific provider — so adding a
new provider later (Microsoft, Slack, etc.) means writing one new
subclass and registering it, not editing the view code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Optional

# Intent of the OAuth flow:
#   "login"   — flow is initiated from a signed-out user on the sign-in
#               page; success creates or signs in a user.
#   "connect" — flow is initiated from a signed-in user wanting to
#               attach API access; success adds (or refreshes) a
#               ConnectedAccount on the current user.
FlowIntent = Literal["login", "connect"]


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: Optional[str]
    expires_in_seconds: Optional[int]
    granted_scopes: list[str]


@dataclass(frozen=True)
class ProviderProfile:
    provider_user_id: str
    email: Optional[str]
    display_name: Optional[str]
    # Whether the PROVIDER vouches that this account's owner controls `email`.
    # Default False so a provider that forgets to say fails closed: an address
    # nobody proved is a claim, not an identity (MIP-1766).
    email_verified: bool = False


class InvalidGrantError(Exception):
    """The provider rejected a refresh token (revoked, expired or already used).

    For a provider that reports it as HTTP 200 with an `error` body, as GitHub does, where
    `requests.HTTPError` is never raised. `tokens.get_valid_access_token` turns it into
    `ReauthRequired`, like the standard HTTP 400 `invalid_grant`.
    """


class OAuthProvider(ABC):
    """Minimal interface a concrete provider must implement."""

    name: str  # "google" | "github" | "github_app"

    # Whether `oauth_views` serves this provider's initiate / callback / disconnect routes. False
    # for a provider whose flow is not wired yet (`github_app`, MIP-2086): it can refresh and
    # revoke tokens, but nobody can start a sign-in or connect with it.
    has_flow: bool = True

    # True when a refresh makes the OLD refresh token (and access token) stop working, as GitHub
    # does. Two concurrent refreshes would then lose the connection, so `tokens` refreshes such a
    # provider under a row lock.
    rotates_refresh_token: bool = False

    @abstractmethod
    def authorize_url(self, *, state: str, intent: FlowIntent, redirect_uri: str) -> str:
        """Build the provider's consent URL. The browser is redirected here."""

    @abstractmethod
    def exchange_code(self, *, code: str, redirect_uri: str) -> TokenResponse:
        """Exchange an authorization code for an access (+ refresh) token."""

    @abstractmethod
    def refresh(self, *, refresh_token: str) -> TokenResponse:
        """Use a refresh token to get a fresh access token.
        For providers whose tokens don't expire (e.g. GitHub OAuth Apps)
        this can raise NotImplementedError — callers should check token
        type via `supports_refresh` before invoking."""

    @abstractmethod
    def fetch_profile(self, *, access_token: str) -> ProviderProfile:
        """Read the provider's "who am I" endpoint with the access token."""

    @property
    def supports_refresh(self) -> bool:
        """True if access tokens expire and need refreshing."""
        return True
