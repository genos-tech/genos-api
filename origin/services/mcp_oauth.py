"""Policy for the MCP OAuth authorization server (MIP-1077).

The library (django-oauth-toolkit) does the protocol. This is what is Genos's own: whether it
is on, where it points, and what a client is allowed to register.

**Registration is open** (the MCP spec requires either Client ID Metadata Documents or Dynamic
Client Registration, and claude.ai and ChatGPT register themselves), so what a stranger can ask
for is narrowed here: a public client (no secret), the authorization-code grant with refresh, and
redirect addresses that are `https`, or `http` only to a loopback address. Anything else is
refused with the RFC 7591 error a client expects. Only a whitelist of fields is passed on to the
library, so a registration cannot set anything we did not decide to allow.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings

from apis.mcp_oauth_settings import MCP_PATH, SCOPES

MAX_BODY_BYTES = 8192
MAX_REDIRECT_URIS = 5
MAX_REDIRECT_URI_CHARS = 2000
MAX_CLIENT_NAME_CHARS = 100
ALLOWED_GRANTS = ("authorization_code", "refresh_token")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def enabled() -> bool:
    return bool(getattr(settings, "MCP_OAUTH_ENABLED", False)) and bool(public_base())


def public_base() -> str:
    return (getattr(settings, "API_PUBLIC_BASE_URL", "") or "").rstrip("/")


def resource_identifier() -> str:
    """The one resource an access token is for: the MCP endpoint (RFC 8707)."""
    return f"{public_base()}{MCP_PATH}"


def consent_url() -> str:
    """Where a person approves a connection: a route of the Genos app, fixed by setting,
    never taken from the request (so the authorize endpoint cannot be used as an open redirect)."""
    return f"{settings.FRONTEND_BASE_URL.rstrip('/')}/oauth/consent"


def redirect_uri_problem(uri: Any) -> str | None:
    """Why `uri` may not be a client's redirect address, or None.

    `https` anywhere, or `http` to a loopback address (a desktop client's own callback). No
    fragment (RFC 6749), no wildcard, no credentials, no custom schemes."""
    if not isinstance(uri, str) or not uri:
        return "a redirect URI must be a non-empty string"
    if len(uri) > MAX_REDIRECT_URI_CHARS:
        return "a redirect URI is too long"
    if _CONTROL.search(uri) or any(c.isspace() for c in uri):
        return "a redirect URI must not contain spaces or control characters"
    if "*" in uri:
        return "a redirect URI must not contain a wildcard"
    try:
        parts = urlsplit(uri)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return "a redirect URI is not a valid URL"
    if parts.fragment or "#" in uri:
        return "a redirect URI must not contain a fragment"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "a redirect URI must not contain credentials"
    if not host:
        return "a redirect URI must name a host"
    if parts.scheme == "https":
        return None
    if parts.scheme == "http" and host in LOOPBACK_HOSTS:
        return None
    return "a redirect URI must be https, or http to localhost or a loopback address"


def check_registration(data: Any) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    """A client's registration request -> `(clean_document, None)` or `(None, (error, description))`.

    The clean document holds only fields we allow, with the defaults a public client gets, and is
    what the library is handed."""
    if not isinstance(data, dict):
        return None, ("invalid_client_metadata", "The request body must be a JSON object.")

    uris = data.get("redirect_uris")
    if not isinstance(uris, list) or not uris:
        return None, ("invalid_redirect_uri", "redirect_uris must be a non-empty list.")
    if len(uris) > MAX_REDIRECT_URIS:
        return None, (
            "invalid_redirect_uri",
            f"At most {MAX_REDIRECT_URIS} redirect URIs may be registered.",
        )
    for uri in uris:
        problem = redirect_uri_problem(uri)
        if problem:
            return None, ("invalid_redirect_uri", problem[0].upper() + problem[1:] + ".")
    if len(set(uris)) != len(uris):
        return None, ("invalid_redirect_uri", "A redirect URI is listed twice.")

    method = data.get("token_endpoint_auth_method", "none")
    if method != "none":
        return None, (
            "invalid_client_metadata",
            "Only public clients are supported: token_endpoint_auth_method must be 'none'.",
        )

    grants = data.get("grant_types", list(ALLOWED_GRANTS))
    if (
        not isinstance(grants, list)
        or not all(isinstance(g, str) for g in grants)
        or "authorization_code" not in grants
        or not set(grants) <= set(ALLOWED_GRANTS)
    ):
        return None, (
            "invalid_client_metadata",
            "grant_types must include authorization_code and may add refresh_token; nothing else is supported.",
        )

    response_types = data.get("response_types", ["code"])
    if response_types != ["code"]:
        return None, ("invalid_client_metadata", "response_types must be ['code'].")

    scope = data.get("scope")
    if scope is not None:
        if not isinstance(scope, str) or not set(scope.split()) <= set(SCOPES):
            return None, (
                "invalid_client_metadata",
                "scope may only name: " + ", ".join(sorted(SCOPES)) + ".",
            )

    name = data.get("client_name", "")
    if not isinstance(name, str):
        return None, ("invalid_client_metadata", "client_name must be a string.")
    name = " ".join(_CONTROL.sub(" ", name).split())
    if len(name) > MAX_CLIENT_NAME_CHARS:
        return None, (
            "invalid_client_metadata",
            f"client_name is at most {MAX_CLIENT_NAME_CHARS} characters.",
        )

    return {
        "redirect_uris": list(uris),
        "client_name": name,
        "grant_types": ["authorization_code"]
        + (["refresh_token"] if "refresh_token" in grants else []),
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, None
