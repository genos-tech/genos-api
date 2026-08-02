"""`POST /api/public/v1/mcp` — the endpoint.

Auth, team resolution, then JSON-RPC dispatch. The tools do their own
ACL work, so this layer's job is narrow: establish *who* is calling and
*which team* they're calling about, and never let either be asserted by
the caller.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from rest_framework.response import Response

from origin.models.common.api_key_models import SCOPE_READ
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.metered import metered_request
from origin.search_engine.quota import get_mcp_enabled
from origin.views.public.mcp import protocol, tools
from origin.views.public.mcp.protocol import JsonRpcError
from origin.views.public.permissions import HasApiKey, PublicApiThrottle
from origin.views.public.v1_views import PublicApiView
from origin.views.utils.scope_guards import is_guest, is_team_member

log = logging.getLogger(__name__)


class McpView(PublicApiView):
    """MCP over Streamable HTTP, stateless.

    Inherits `PublicApiView` for `resolve_team` and the per-key throttle,
    but **replaces its permission set**, and that is the one surprising
    thing in this file:

    `HasRequiredScope` decides read-vs-write from the HTTP method. Every
    MCP message is a POST — `tools/list` and a read-only `tools/call`
    included — so inheriting it unchanged would 403 a read-scoped key on
    every request it ever makes, including pure reads. Scope is therefore
    enforced per *tool* in `tools.call`, which is the only place that
    knows whether a given message writes.

    That forfeits the safe-by-default property `permissions.py` is built
    around, where a new endpoint inherits the rule instead of having to
    remember it. The compensation is that there is no "new endpoint"
    here: one view, one dispatcher, and a write tool cannot reach the
    registry without passing the scope check in `tools.call`.
    """

    permission_classes = [HasApiKey]
    throttle_classes = [PublicApiThrottle]
    # POST only. Earlier MCP revisions opened a standalone SSE stream with
    # GET and ended sessions with DELETE; this revision has neither, and
    # the spec says to answer both with 405. Claude Code still tries the
    # GET and carries on unbothered when it gets one.
    http_method_names = ["post", "options"]

    def post(self, request):
        try:
            self._check_origin(request)
            body = self._parse(request)
            return self._dispatch(request, body)
        except JsonRpcError as exc:
            request_id = None
            if isinstance(getattr(request, "data", None), dict):
                request_id = request.data.get("id")
            return Response(exc.as_response(request_id), status=exc.http_status)

    # -- transport concerns ------------------------------------------------

    def _check_origin(self, request) -> None:
        """Reject a cross-origin browser caller.

        A spec MUST, aimed at DNS rebinding: a page on the open web
        resolving a hostname to a private address and then driving a
        local MCP server. Genuine MCP clients are not browsers and send
        no `Origin` at all — Claude Code doesn't — so the check only ever
        fires on something that is a browser and shouldn't be here.
        """
        origin = request.headers.get("Origin")
        if not origin:
            return
        allowed = set(getattr(settings, "CORS_ALLOWED_ORIGINS", []) or [])
        if origin in allowed:
            return
        host = (urlparse(origin).hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            return
        raise JsonRpcError(
            protocol.INVALID_REQUEST,
            f"Origin {origin} is not allowed.",
            http_status=403,
        )

    def _parse(self, request) -> dict[str, Any]:
        body = request.data
        if isinstance(body, (bytes, str)):
            try:
                body = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise JsonRpcError(protocol.PARSE_ERROR, "Invalid JSON.", http_status=400)
        if not isinstance(body, dict):
            # A batch (JSON array) lands here too. Batching was removed in
            # the current revision and no client we serve sends one.
            raise JsonRpcError(
                protocol.INVALID_REQUEST,
                "Expected a single JSON-RPC request object.",
                http_status=400,
            )
        return body

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self, request, body: dict[str, Any]) -> Response:
        method = body.get("method")
        params = body.get("params") or {}
        modern = protocol.is_modern(body)

        # Branch on the *absence of an id*, not on the method name: that
        # is what makes a JSON-RPC message a notification, and the rule
        # has to hold for a notification we've never heard of.
        # `notifications/initialized` arrives this way from every legacy
        # client, and answering it with a JSON-RPC envelope — even a
        # correct-looking one — violates the protocol.
        if "id" not in body:
            return Response(status=202)

        request_id = body.get("id")

        if modern:
            protocol.validate_headers(body, request.headers)
        declared = protocol.requested_version(body)
        if declared is not None and declared not in protocol.SUPPORTED_VERSIONS:
            raise protocol.unsupported_version(declared)

        if method == "initialize":
            return self._ok(request_id, protocol.initialize_result(params), modern=modern)

        if method == "ping":
            return self._ok(request_id, {}, modern=modern)

        if method == "server/discover":
            return self._ok(request_id, self._discover(), modern=modern)

        # Everything past here needs to know who is asking and about what.
        ctx, scope = self._context(request)

        if method == "tools/list":
            listed = [tools.describe(t) for t in tools.visible_tools(scope)]
            return self._ok(request_id, {"tools": listed}, modern=modern)

        if method == "tools/call":
            return self._ok(
                request_id,
                self._call_tool(params, ctx, scope),
                modern=modern,
            )

        raise protocol.method_not_found(str(method), modern=modern)

    def _ok(self, request_id: Any, payload: dict[str, Any], *, modern: bool) -> Response:
        return Response(protocol.envelope(request_id, protocol.result(payload, modern=modern)))

    def _discover(self) -> dict[str, Any]:
        return {
            "supportedVersions": list(protocol.SUPPORTED_VERSIONS),
            "capabilities": {"tools": {}},
            "_meta": {"io.modelcontextprotocol/serverInfo": dict(protocol.SERVER_INFO)},
            "instructions": protocol.initialize_result({})["instructions"],
        }

    # -- identity ----------------------------------------------------------

    def _context(self, request) -> tuple[ToolContext, str]:
        """Establish the server-trusted `(team, user)` every tool assumes.

        Every tool docstring calls `ctx.team_id` "server-trusted", which
        is only true if something checks it. `resolve_team` already
        refuses a team a personal token's owner doesn't belong to; the
        membership gate is then re-run for *both* kinds of key, because a
        team-bound key keeps naming its team after its owner has been
        removed from it.
        """
        team = self.resolve_team(request)
        if team is None:
            raise JsonRpcError(
                protocol.INVALID_PARAMS,
                "No team for this request. A personal access token must name "
                "one with ?team_id=<uuid> on the server URL; a team-scoped key "
                "carries its own.",
                http_status=400,
            )

        user_id = str(request.user.id)
        team_id = str(team.team_id)
        if not is_team_member(team_id, user_id) and not is_guest(team_id, user_id):
            raise JsonRpcError(
                protocol.INVALID_PARAMS,
                "Team not found.",
                http_status=404,
            )

        # MCP is a Pro-and-up capability. Checked HERE because this is
        # the one place both `tools/list` and `tools/call` pass through,
        # so neither can be reached without it — a gate on the two call
        # sites is a gate someone forgets when a third arrives.
        #
        # `initialize`, `ping` and `server/discover` deliberately stay
        # open: they carry no workspace data, and answering them lets the
        # client complete its handshake and SHOW this message instead of
        # reporting a bare connection failure.
        if not get_mcp_enabled(user_id):
            raise JsonRpcError(
                protocol.INVALID_REQUEST,
                "MCP is available on the Pro plan and above. Your current plan "
                "doesn't include it — upgrade in Genos under "
                "Settings → Plan & Usage, and this connection will start "
                "working without any change here. The REST API at "
                "/api/public/v1/ is available on every plan.",
                http_status=403,
            )

        scope = getattr(request.auth, "scope", SCOPE_READ)
        return ToolContext(team_id=team_id, user_id=user_id), scope

    def _call_tool(self, params: dict[str, Any], ctx: ToolContext, scope: str) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(
                protocol.INVALID_PARAMS, "`arguments` must be an object.", http_status=400
            )

        def unknown(tool_name: str) -> Exception:
            available = ", ".join(t.name for t in tools.visible_tools(scope))
            return JsonRpcError(
                protocol.INVALID_PARAMS,
                f"Unknown tool: {tool_name}. Available: {available}.",
            )

        # `search_workspace` embeds its query, which costs money. Bound the
        # spend to a named surface so it lands on this team's ledger
        # instead of `unattributed`.
        #
        # The tool runs inline, on the request thread, deliberately:
        # `TaskActivity.actor` is read from a thread-local that
        # `CurrentUserMiddleware` sets per request. Hand this to a worker
        # thread and every task Claude edits shows up in the activity feed
        # with no author.
        with metered_request(surface="mcp", user_id=ctx.user_id, team_id=ctx.team_id):
            try:
                return tools.call(name, arguments, ctx, scope, on_unknown_tool=unknown)
            except JsonRpcError:
                raise
            except Exception:  # noqa: BLE001 — an unexpected bug is ours, not the caller's
                log.exception("MCP tool %s failed", name)
                raise JsonRpcError(
                    protocol.INTERNAL_ERROR,
                    f"`{name}` failed unexpectedly. This is a bug in Genos, not in your request.",
                )
