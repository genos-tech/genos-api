"""The OAuth 2.1 authorization server's settings for MCP (MIP-1077).

django-oauth-toolkit's defaults are permissive: refresh-token reuse protection is off, tokens
live ten hours and are stored in the clear, an access token can ride in a URL, `plain` PKCE is
allowed. Every security-relevant setting is therefore stated here, on purpose, and
`test_mcp_oauth_foundation` pins each one, so a library upgrade or a careless edit cannot
quietly loosen one.

This is a PURE module (no Django imports) because `settings.py` builds the dict at import time,
before apps are loaded, and the tests build it again for a base URL of their own.

**Anchored to a configured address, never to the request.** `OIDC_ISS_ENDPOINT` is the setting
DOT uses to build every endpoint URL in the metadata (and it is returned verbatim as the
issuer). Left unset, those URLs come from the `Host` and forwarded headers, so a request
carrying a hostile `Host` could be given metadata pointing at a token endpoint it controls.
It is set to the API's own public address, so the metadata can only name us.
"""

from __future__ import annotations

MCP_PATH = "/api/public/v1/mcp"

SCOPE_READ = "mcp:read"
SCOPE_WRITE = "mcp:write"
SCOPE_DESTRUCTIVE = "mcp:destructive"

SCOPES = {
    SCOPE_READ: "Read your Genos workspace",
    SCOPE_WRITE: "Make changes in your Genos workspace",
    SCOPE_DESTRUCTIVE: "Delete things in your Genos workspace (this cannot be undone)",
}

ACCESS_TOKEN_SECONDS = 60 * 60
REFRESH_TOKEN_SECONDS = 60 * 24 * 60 * 60
AUTHORIZATION_CODE_SECONDS = 60


def provider_settings(public_base: str) -> dict:
    """The `OAUTH2_PROVIDER` dict for an API whose public origin is `public_base`."""
    base = (public_base or "").rstrip("/")
    return {
        # --- Who may ask, and how ---------------------------------------------------------
        # Public clients only (a client on someone's laptop or in a vendor's cloud cannot keep a
        # secret), proving themselves with PKCE.
        "PKCE_REQUIRED": True,
        "OAUTH2_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED": ["none"],
        "OAUTH2_GRANT_TYPES_SUPPORTED": ["authorization_code", "refresh_token"],
        "OAUTH2_RESPONSE_TYPES_SUPPORTED": ["code"],
        # `https`, and `http` only for a loopback address: `redirect_uri_problem` in
        # `services/mcp_oauth.py` enforces that at registration, and DOT matches exactly at use.
        "ALLOWED_REDIRECT_URI_SCHEMES": ["https", "http"],
        # A desktop client that registered `http://localhost:<port>/...` may arrive on a
        # different port (RFC 8252 section 8.3).
        "ALLOW_LOCALHOST_LOOPBACK": True,
        # --- Codes and tokens -------------------------------------------------------------
        "AUTHORIZATION_CODE_EXPIRE_SECONDS": AUTHORIZATION_CODE_SECONDS,
        "ACCESS_TOKEN_EXPIRE_SECONDS": ACCESS_TOKEN_SECONDS,
        "REFRESH_TOKEN_EXPIRE_SECONDS": REFRESH_TOKEN_SECONDS,
        # A refresh token is single use. Presenting an old one again means it was stolen, and
        # revokes the whole family. No grace period: DOT refuses to start with hashed storage
        # and a grace period together.
        "ROTATE_REFRESH_TOKEN": True,
        "REFRESH_TOKEN_REUSE_PROTECTION": True,
        "REFRESH_TOKEN_GRACE_PERIOD_SECONDS": 0,
        # --- RFC 9700 (OAuth security best current practice), every gate on ----------------
        "COMPLIANT_BCP_RFC9700_IMPLICIT_GRANT": True,
        "COMPLIANT_BCP_RFC9700_PASSWORD_GRANT": True,
        "COMPLIANT_BCP_RFC9700_PKCE_METHOD": True,  # S256 only
        "COMPLIANT_BCP_RFC9700_PKCE_REQUIRED": True,
        "COMPLIANT_BCP_RFC9700_ACCESS_TOKEN_TRANSPORT": True,  # never in a URL
        "COMPLIANT_BCP_RFC9700_AUTHZ_RESPONSE_ISS": True,  # RFC 9207 `iss`
        "COMPLIANT_BCP_RFC9700_TOKEN_STORAGE": True,  # hashed at rest
        "COMPLIANT_BCP_RFC9700_REFRESH_TOKEN": True,
        "COMPLIANT_BCP_RFC9700_REDIRECT_URI_SCHEME": True,
        "COMPLIANT_BCP_RFC9700_REDIRECT_URI_MATCHING": True,
        # --- Scopes: what the member allows the connection to do ---------------------------
        "SCOPES": dict(SCOPES),
        "DEFAULT_SCOPES": [SCOPE_READ],
        "READ_SCOPE": SCOPE_READ,
        "WRITE_SCOPE": SCOPE_WRITE,
        # --- Discovery, anchored to our own address (see the module docstring) -------------
        "OIDC_ISS_ENDPOINT": base,
        "OAUTH2_PROTECTED_RESOURCE_IDENTIFIER": f"{base}{MCP_PATH}" if base else "",
        "OAUTH2_PROTECTED_RESOURCE_AUTHORIZATION_SERVERS": [base] if base else [],
        "OAUTH2_PROTECTED_RESOURCE_NAME": "Genos MCP",
        # --- Registering a client: both ways the MCP spec names -----------------------------
        # CIMD: the client_id is the https URL of a metadata document the client hosts. DOT
        # fetches it with an SSRF guard, a size cap, a timeout and a failure back-off.
        "CIMD_ENABLED": True,
        "CIMD_FETCH_TIMEOUT_SECONDS": 3,
        "CIMD_MAX_DOCUMENT_SIZE": 8192,
        # DCR (RFC 7591): open, as the spec requires, but only through our wrapper view
        # (`views/public/oauth_views.py`), which rate-limits and enforces what a client may
        # register. DOT's own view is never mounted directly.
        "DCR_ENABLED": True,
        "DCR_REGISTRATION_PERMISSION_CLASSES": ("oauth2_provider.dcr.AllowAllDCRPermission",),
    }
