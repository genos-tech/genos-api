"""Provider registry — the one place to look up an OAuthProvider by
name. Keeps `oauth_views.py` free of conditionals like
`if provider_name == "google": ...`.
"""

from .base import OAuthProvider
from .github import GitHubOAuthProvider
from .github_app import GitHubAppOAuthProvider
from .google import GoogleOAuthProvider

_REGISTRY: dict[str, OAuthProvider] = {
    "google": GoogleOAuthProvider(),
    "github": GitHubOAuthProvider(),
    "github_app": GitHubAppOAuthProvider(),
}


def get_provider(name: str) -> OAuthProvider:
    """Any registered provider, including one whose browser flow is not wired (token refresh and
    revoke look a stored row's provider up through this)."""
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"Unknown OAuth provider: {name!r}") from exc


def supported_provider_names() -> list[str]:
    """The providers the sign-in / connect / disconnect routes serve: those with a wired flow."""
    return [name for name, provider in _REGISTRY.items() if provider.has_flow]
