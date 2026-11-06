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
from oauth2_provider.cimd import is_cimd_client_id

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


# RFC 7636 §4.2: 43 to 128 characters of the unreserved set (an S256 challenge is 43).
_CHALLENGE = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
MAX_CONSENT_TEAMS = 50


def authorize_problem(method: Any, challenge: Any, resources: list[str]) -> tuple[str, str] | None:
    """Why an authorization request, valid for the library, is still not one Genos grants.

    PKCE with S256 only, checked up front: the library treats a missing method as `plain` and
    refuses that only when the code is issued, after the member has been asked. And RFC 8707: a
    token is for one resource, ours. One that names another is `invalid_target`; one that names
    none is given ours (there is only one to give)."""
    if method != "S256" or not isinstance(challenge, str) or not _CHALLENGE.fullmatch(challenge):
        return "invalid_request", "PKCE with code_challenge_method=S256 is required."
    for resource in resources:
        if resource != resource_identifier():
            return "invalid_target", "This server only issues tokens for its MCP endpoint."
    return None


def authorize_query(params: Any) -> tuple[str | None, str | None]:
    """The query string of an authorization request posted by the consent page, or why not.

    The page sends the parameters it was given, as an object of strings (`resource` may repeat,
    as a list). They are turned back into a query for the library to validate exactly as if the
    client had sent it, so nothing the page says is trusted: the server decides again."""
    from urllib.parse import urlencode

    if not isinstance(params, dict) or not params or len(params) > 20:
        return None, "The authorization request is missing."
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        values = value if key == "resource" and isinstance(value, list) else [value]
        for item in values:
            if not isinstance(key, str) or not isinstance(item, str) or len(item) > 2048:
                return None, "The authorization request is malformed."
            pairs.append((key, item))
    return urlencode(pairs), None


def check_scopes(scopes: Any, allowed: list[str]) -> tuple[list[str] | None, str | None]:
    """The scopes a member chose, in our order; or why it is refused.

    Every name must be one of `allowed`: asking for more than the client asked for (or an
    unknown name) is refused, not quietly trimmed. Always with `mcp:read` (a connection that
    cannot read is no connection); deleting implies write."""
    if not isinstance(scopes, list) or not all(isinstance(x, str) for x in scopes):
        return None, "scopes must be a list of names."
    if any(x not in allowed for x in scopes):
        return None, "That permission is not available."
    granted = [x for x in SCOPES if x in scopes]
    if "mcp:read" not in granted:
        return None, "A connection needs at least read access."
    if "mcp:destructive" in granted and "mcp:write" not in granted:
        return None, "Deleting needs write access too."
    return granted, None


def check_team_ids(team_ids: Any) -> tuple[list[str] | None, str | None]:
    """1 to 50 ids, deduped, in the order given; or why it is refused. Whether the member is
    in them is the view's check, against the database."""
    if (
        not isinstance(team_ids, list)
        or not team_ids
        or not all(isinstance(x, str) for x in team_ids)
        or len(team_ids) > MAX_CONSENT_TEAMS
    ):
        return None, "Choose at least one team."
    return list(dict.fromkeys(team_ids)), None


def check_decision(body: dict[str, Any], requested: list[str]) -> tuple[dict | None, str | None]:
    """What a member allowed, from the consent page's body, or why it is refused.

    `scopes`: a subset of what the client asked for. `team_ids` with `default_team_id` among
    them."""
    granted, why = check_scopes(body.get("scopes", requested), requested)
    if why:
        return None, why
    ordered, why = check_team_ids(body.get("team_ids"))
    if why:
        return None, why
    default_team_id = body.get("default_team_id")
    if default_team_id not in ordered:
        return None, "The default team must be one of the chosen teams."
    return {"scopes": granted, "team_ids": ordered, "default_team_id": default_team_id}, None


def client_problem(application: Any) -> str | None:
    """Why this registered client may not be used, or None.

    A client registered by DCR was held to this when it registered; one that is a metadata
    document is published by a stranger and was not, so the same limits are applied to the
    client itself, whichever way it came: a public client, the authorization-code grant only,
    and no redirect address that `redirect_uri_problem` refuses (https, or loopback http)."""
    if application.client_type != "public":
        return "Only public clients are supported."
    if application.authorization_grant_type != "authorization-code":
        return "Only the authorization-code grant is supported."
    for uri in (application.redirect_uris or "").split():
        problem = redirect_uri_problem(uri)
        if problem:
            return f"The client's registration is not acceptable: {problem}."
    return None


def client_summary(application: Any) -> dict[str, str]:
    """Who a client is, as far as Genos can tell the member.

    `registered`: it registered itself, and anyone can, under any name: the name proves nothing.
    `metadata_document`: its client_id is an address, and `host` is that address's host, which
    does prove who published it."""
    document = is_cimd_client_id(application.client_id)
    # The name is the client's own words (a registration, or a document anyone can publish):
    # one line, short, no control characters, before any page shows it.
    name = _CONTROL.sub(" ", application.name or "")
    return {
        "name": " ".join(name.split())[:MAX_CLIENT_NAME_CHARS],
        "kind": "metadata_document" if document else "registered",
        "host": (urlsplit(application.client_id).hostname or "") if document else "",
    }
