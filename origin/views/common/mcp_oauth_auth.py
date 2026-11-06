"""OAuth access-token authentication for the MCP endpoint (MIP-1077).

    Authorization: Bearer <access token issued by /oauth/token/>

The sibling of `ApiKeyAuthentication`, and it has the same job and the same trap: **nothing
transfers from the token library.** It would happily call a token valid after the person was
deactivated, after they disconnected the app, or when it was issued for another service. So
every one of these is checked here, on every request:

  * OAuth is switched on (`MCP_OAUTH_ENABLED`): off, this authenticator is silent, so the
    switch also kills tokens already issued;
  * the token exists (looked up by the hash the library stores), has not expired, and carries
    `mcp:read`;
  * **it was issued for THIS resource.** The library treats a token with no resource as valid
    anywhere; the consent step always binds one, and a token without ours is refused (RFC 8707,
    and the MCP spec's rule that a server must not accept a token meant for another);
  * the member's connection for that app is still live (revoking it ends every token at once);
  * the person is active and not deleted.

What the token may DO is not in the token: see `mcp_credential`.

A token that IS ours and is refused raises, never returns None: None means "not my credential"
and would let the request fall through and end up anonymous. One message for every refusal; the
security log keeps the reason.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta

from django.utils import timezone
from oauth2_provider.models import get_access_token_model
from rest_framework import authentication, exceptions

from origin.models.common.mcp_connection_models import McpConnection
from origin.services import mcp_oauth, security_events
from origin.services.mcp_credential import McpOAuthCredential

KEYWORD = "Bearer"
MESSAGE = "Invalid or expired access token."

# Tokens are url-safe random strings of a few dozen characters. Refusing anything else before
# the database is touched keeps a pile of junk Bearer values (a JWT, a scanner's noise) cheap.
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9._~+/=-]{20,512}")

# Like an API key's `last_used_at`: a debugging affordance, not an audit record.
_LAST_USED_RESOLUTION = timedelta(hours=1)


def _django(request):
    """The Django request under DRF's (the audit writer reads `.auth` and `.user`, which on the
    DRF request would re-enter this authenticator)."""
    return getattr(request, "_request", request)


class McpOAuthAuthentication(authentication.BaseAuthentication):
    keyword = KEYWORD

    def authenticate(self, request):
        """The connected app behind a Bearer token, or None when it is not one of ours.

        None is for "not my credential": OAuth is off, no Bearer, or a value that is no token
        of ours (a session JWT, which MCP has always met with the permission's own refusal, or
        junk, which the next authenticator rejects). Once a token IS ours, anything wrong with
        it raises."""
        if not mcp_oauth.enabled():
            return None
        auth = authentication.get_authorization_header(request).split()
        if len(auth) != 2 or auth[0].lower() != self.keyword.lower().encode():
            return None
        try:
            raw = auth[1].decode()
        except UnicodeError:
            return None
        if not _TOKEN_SHAPE.fullmatch(raw):
            return None
        django_request = _django(request)

        token = (
            get_access_token_model()
            .objects.select_related("application", "user")
            .filter(token_checksum=hashlib.sha256(raw.encode()).hexdigest())
            .first()
        )
        if token is None:
            return None

        connection = (
            McpConnection.objects.select_related("application", "user", "default_team")
            .filter(user_id=token.user_id, application_id=token.application_id)
            .first()
            if token.user_id and token.application_id
            else None
        )
        reason = self._refusal(token, connection)
        if reason:
            security_events.note_mcp_rejected(django_request, reason, connection)
            raise exceptions.AuthenticationFailed(MESSAGE)

        self._touch(connection)
        credential = McpOAuthCredential(connection)
        security_events.note_mcp_used(django_request, credential)
        return (connection.user, credential)

    @staticmethod
    def _refusal(token, connection) -> str | None:
        """Why this token may not be used, or None."""
        if token.is_expired():
            return "expired"
        if "mcp:read" not in (token.scope or "").split():
            return "scope"
        resources = token.resource or []
        if resources != [mcp_oauth.resource_identifier()]:
            # Empty = unrestricted in the library; more than one = also valid elsewhere.
            return "audience"
        if connection is None or not connection.is_active:
            return "connection_revoked"
        user = connection.user
        if user is None or not user.is_active or getattr(user, "is_deleted", False):
            return "account_inactive"
        return None

    @staticmethod
    def _touch(connection: McpConnection) -> None:
        now = timezone.now()
        if (
            connection.last_used_at is not None
            and now - connection.last_used_at < _LAST_USED_RESOLUTION
        ):
            return
        McpConnection.objects.filter(pk=connection.pk).update(last_used_at=now)

    def authenticate_header(self, request):
        # The header MCP has always answered with (SimpleJWT's), so a client that knows no
        # better sees no change. When OAuth is set up the view replaces it with the
        # `resource_metadata` challenge.
        return f'{self.keyword} realm="api"'
