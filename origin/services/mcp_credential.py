"""A connected app's access token, as the `Credential` MCP works with (MIP-1077).

Built on every request from the member's `McpConnection`, never from the token: the token only
says WHICH connection; what it may do is read here from the connection as it is now. So revoking
the connection, turning off deleting, or narrowing its teams applies on the very next call, with
nothing in a token to go stale.
"""

from __future__ import annotations

from typing import Any

from origin.models.common.mcp_connection_models import McpConnection
from origin.services.mcp_client_family import app_family


class McpOAuthCredential:
    kind = "oauth"

    def __init__(self, connection: McpConnection):
        self.connection = connection
        self.id = connection.id
        self.user = connection.user
        self.scope = connection.scope
        self.allow_destructive = connection.allow_destructive

    @property
    def pinned_team_id(self) -> str | None:
        # Not bound to one team: it works in whichever granted team a call names.
        return None

    @property
    def allowed_team_ids(self) -> frozenset[str]:
        return frozenset(str(t) for t in (self.connection.team_ids or []))

    @property
    def default_team_id(self) -> str | None:
        return str(self.connection.default_team_id) if self.connection.default_team_id else None

    @property
    def label(self) -> str:
        app = self.connection.application
        return (app.name or app.client_id)[:100]

    def throttle_ident(self) -> str:
        return f"oauth:{self.id}"

    def audit_ref(self) -> str:
        return f"oauth:{self.id}"

    def via(self) -> dict[str, Any]:
        via: dict[str, Any] = {
            "kind": "oauth",
            "connection_id": str(self.id),
            "client_name": self.label,
        }
        # The official family (MIP-1568), from the client's registered redirect URIs and never
        # from its name; stamped with the write, so a row keeps the family of that moment.
        if family := app_family(self.connection.application):
            via["app"] = family
        return via
