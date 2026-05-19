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


class OAuthProvider(ABC):
    """Minimal interface a concrete provider must implement."""

    name: str  # "google" | "github"

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
