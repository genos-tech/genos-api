"""The JSON-RPC envelope, and the two protocol eras we answer to.

MCP split in two at revision `2026-07-28`. Everything up to `2025-11-25`
("legacy") opens with an `initialize` handshake and keeps the negotiated
version for the connection. From `2026-07-28` ("modern") there is no
handshake at all: every request carries its own version in
`params._meta`, mirrored into HTTP headers, and the server answers each
one independently.

We speak both, which the spec calls "dual-era" and explicitly allows on
one endpoint. That is not hedging:

  * **Legacy is what actually connects today.** Claude Code 2.1.201 sends
    `initialize` with `protocolVersion: "2025-11-25"`, then
    `notifications/initialized`, then a GET for a stream (it accepts our
    405), then `tools/list`. That transcript is why the legacy path is
    the complete one.
  * **Modern is fall-forward insurance.** The spec's compatibility matrix
    has exactly one broken cell — a modern client against a legacy-only
    server — and it has no client-side fallback. A Claude Code upgrade
    would simply stop working. Answering modern requests costs a few
    branches and removes that cliff.

We deliberately implement none of the optional modern machinery: no
sessions, no resumable streams, no MRTR, no `subscriptions/listen`, no
pagination. A tools-only server needs none of it, and each one would be
a feature to maintain for no caller.

Statelessness is a runtime constraint, not a preference. The API runs on
gunicorn `gthread` with 8 threads and `--max-requests 1000`; a held-open
SSE stream would occupy one of those eight slots and then be killed
mid-flight on recycle. So every response here is a single JSON object.
"""

from __future__ import annotations

from typing import Any

# Ordered newest-first: this is what we advertise in an
# `UnsupportedProtocolVersionError`, and a client picks from the top.
SUPPORTED_VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26")
MODERN_VERSIONS = frozenset({"2026-07-28"})
# What we answer `initialize` with when the client asks for something we
# don't recognise. The newest legacy revision, since only a legacy client
# sends `initialize` at all.
DEFAULT_LEGACY_VERSION = "2025-11-25"

_META_VERSION = "io.modelcontextprotocol/protocolVersion"

SERVER_INFO = {
    "name": "genos",
    "title": "Genos",
    "version": "1.0.0",
    "websiteUrl": "https://genosai.dev",
}

# JSON-RPC 2.0 reserved codes, plus the two MCP allocates.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022


class JsonRpcError(Exception):
    """A failure that belongs in the JSON-RPC `error` member.

    Distinct from a *tool* error, which is a successful RPC whose result
    carries `isError: true`. The split matters to the caller: a protocol
    error tells the client its request was malformed, while a tool error
    is fed back to the model to reason about and retry. Conflating them
    means the model never sees the thing it could have fixed.
    """

    def __init__(self, code: int, message: str, *, data: Any = None, http_status: int = 200):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        # JSON-RPC has no status code of its own, and MCP overloads HTTP's:
        # an unknown method is 404 to a modern client but 200 to a legacy
        # one. Carried here so the caller doesn't re-derive it.
        self.http_status = http_status

    def as_response(self, request_id: Any) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


def is_modern(body: dict[str, Any]) -> bool:
    """Which era this single request is written in.

    The body is the source of truth, per the transport spec — the
    `MCP-Protocol-Version` header only mirrors it. Absence means legacy:
    a legacy client sends `tools/list` with no `_meta` at all, so
    defaulting the other way would break the only client we have.
    """
    meta = (body.get("params") or {}).get("_meta") or {}
    return meta.get(_META_VERSION) in MODERN_VERSIONS


def requested_version(body: dict[str, Any]) -> str | None:
    meta = (body.get("params") or {}).get("_meta") or {}
    return meta.get(_META_VERSION)


def result(payload: dict[str, Any], *, modern: bool) -> dict[str, Any]:
    """Build a result body for the era that asked.

    `resultType` arrived with the modern revision. Sending it to a legacy
    client is harmless but wrong, and omitting it from a modern one is a
    schema violation — so every result is built here rather than at the
    call sites, where the distinction would be remembered four times and
    forgotten once.
    """
    if modern:
        return {"resultType": "complete", **payload}
    return payload


def envelope(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def unsupported_version(requested: str) -> JsonRpcError:
    return JsonRpcError(
        UNSUPPORTED_PROTOCOL_VERSION,
        "Unsupported protocol version",
        data={"supported": list(SUPPORTED_VERSIONS), "requested": requested},
        http_status=400,
    )


def method_not_found(method: str, *, modern: bool) -> JsonRpcError:
    return JsonRpcError(
        METHOD_NOT_FOUND,
        f"Method not found: {method}",
        # A modern client reads 404-with-a-JSON-RPC-body as "modern server,
        # unknown method" and 404-without-one as "this isn't an MCP
        # endpoint at all"; the status is load-bearing for that. Legacy
        # clients expect every JSON-RPC reply, error or not, under 200.
        http_status=404 if modern else 200,
    )


def initialize_result(params: dict[str, Any]) -> dict[str, Any]:
    """Answer a legacy handshake.

    Echo the client's version when we know it, so the connection settles
    on what the client already speaks. When we don't, name our newest
    legacy revision instead of failing: a legacy client has no
    fall-forward path, and an answer it can work with beats a correct
    error it cannot act on.
    """
    asked = params.get("protocolVersion")
    version = asked if asked in SUPPORTED_VERSIONS else DEFAULT_LEGACY_VERSION
    return {
        "protocolVersion": version,
        # No `listChanged`: the tool set is fixed at deploy time, and
        # claiming the capability would promise notifications on a stream
        # we deliberately don't hold open.
        "capabilities": {"tools": {}},
        "serverInfo": dict(SERVER_INFO),
        # Written to be true of the server as deployed, not of the server
        # as intended: `content_markdown` is a newer, richer rendering of
        # a task body that older deployments don't return, so the wording
        # prefers it without depending on it. An instruction that promises
        # a field the caller then can't find spends a turn on confusion
        # and teaches it to distrust the rest.
        "instructions": (
            "Genos is the user's workspace: projects, tasks, milestones, notes "
            "and chat. Read a task's description from `content_markdown` when it "
            "is present — it keeps headings, checklists and code fences — and "
            "fall back to `content_text`, which is the same body flattened. A "
            "task can be named by its display id (e.g. PRJ-123), its URL, or its "
            "numeric id. Start from `list_my_tasks` when the user says 'my tasks' "
            "or names no task at all."
        ),
    }


def validate_headers(body: dict[str, Any], headers) -> None:
    """Modern requests mirror body fields into headers; they must agree.

    The spec requires this because intermediaries route on the header
    while the server executes the body — a mismatch is how the two
    disagree about what happened.

    Enforced **only for modern requests**, and that restriction is
    load-bearing: legacy clients send none of these headers, so checking
    unconditionally would 400 every real client we have today. Anyone
    tempted to "fix" this by hoisting the check should re-read the
    transcript in this package's docstring first.
    """
    declared = requested_version(body)
    header_version = headers.get("MCP-Protocol-Version")
    if header_version and header_version != declared:
        raise JsonRpcError(
            HEADER_MISMATCH,
            f"Header mismatch: MCP-Protocol-Version '{header_version}' "
            f"does not match body value '{declared}'.",
            http_status=400,
        )

    method = body.get("method")
    header_method = headers.get("Mcp-Method")
    if header_method and header_method != method:
        raise JsonRpcError(
            HEADER_MISMATCH,
            f"Header mismatch: Mcp-Method '{header_method}' does not match body value '{method}'.",
            http_status=400,
        )

    header_name = headers.get("Mcp-Name")
    if header_name:
        params = body.get("params") or {}
        body_name = params.get("name") or params.get("uri")
        if _decode_header_value(header_name) != body_name:
            raise JsonRpcError(
                HEADER_MISMATCH,
                f"Header mismatch: Mcp-Name '{header_name}' does not match "
                f"body value '{body_name}'.",
                http_status=400,
            )


def _decode_header_value(value: str) -> str:
    """Undo the `=?base64?…?=` sentinel a client uses for values that
    can't ride in a header as-is (non-ASCII, padding, embedded newlines).
    Anything else is already the literal value."""
    if not (value.startswith("=?base64?") and value.endswith("?=")):
        return value
    import base64
    import binascii

    try:
        return base64.b64decode(value[len("=?base64?") : -len("?=")]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        # Undecodable is a mismatch, not a crash — the comparison that
        # follows will reject it with the right error.
        return value
