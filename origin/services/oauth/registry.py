"""Provider registry — the one place to look up an OAuthProvider by
name. Keeps `oauth_views.py` free of conditionals like
`if provider_name == "google": ...`.
"""

from .base import OAuthProvider
from .github import GitHubOAuthProvider
from .google import GoogleOAuthProvider

_REGISTRY: dict[str, OAuthProvider] = {
    "google": GoogleOAuthProvider(),
    "github": GitHubOAuthProvider(),
}


def get_provider(name: str) -> OAuthProvider:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"Unknown OAuth provider: {name!r}") from exc


def supported_provider_names() -> list[str]:
    return list(_REGISTRY.keys())
