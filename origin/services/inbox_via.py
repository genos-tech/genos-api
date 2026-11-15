"""Which connected app made the change an activity inbox item reports (MIP-1569, MIP-1568).

Ken wants the official Claude / ChatGPT icon on notification cards. The one activity item a
connected app's write files today is the notice `project_requests` sends the requester when an MCP
client approves or declines a join request. This module is the whole of what such an item says
about HOW it was made, in `item_optionals["via"]`:

    {"kind": "oauth", "app": "claude" | "chatgpt"}      an official connected app
    {"kind": "oauth"}                                    any other connected app
    (the key is absent)                                  everything else, and every older item

**Stored reduced, always.** The recipient is not the actor, and the row is read by the recipient
alone, so what is stored is already what a colleague may read: the kind and the family. The
connected app's name, connection id and every other field of the full `via` never reach the row
(they stay on the credential and on the actor's own history). Nothing here decides who may see
what: that is `via_for_viewer`'s one rule, applied again when the row is read, so the switch
`SHOW_OAUTH_APP_TO_EVERYONE` acts on the items already filed too.

**A client cannot write one.** `item_optionals` is the client's to fill on the notice endpoint
(`POST /inbox/`) and on the request endpoints; a `via` there would put the official icon on a
message the member wrote by hand. `without_via` drops it at the serializer, the one place those
bodies are stored.
"""

from __future__ import annotations

from typing import Any

from origin.middleware.current_user import get_current_source
from origin.services.mcp_client_family import APP_FAMILIES
from origin.views.public.mcp.via_privacy import via_for_viewer

KEY = "via"


def connected_app_via() -> dict[str, Any] | None:
    """`{"kind": "oauth", "app"?}` for the write in progress when it came through a connected app,
    else None (an API key, the in-app agent, the app itself, a timer or a command).

    The family is the one `McpOAuthCredential.via()` stamped from the registered client; any other
    value (an old credential, a planted one) is dropped. Nothing else of the source is copied."""
    source = get_current_source()
    if not isinstance(source, dict) or source.get("kind") != "oauth":
        return None
    via: dict[str, Any] = {"kind": "oauth"}
    app = source.get("app")
    if isinstance(app, str) and app in APP_FAMILIES:
        via["app"] = app
    return via


def stamped(optionals: dict[str, Any] | None) -> dict[str, Any] | None:
    """`optionals` with the write's connected-app `via` added; unchanged when there is none (so an
    item filed by anyone else, or by a timer, is exactly what it was, `None` included)."""
    via = connected_app_via()
    if via is None:
        return optionals
    return {**(optionals or {}), KEY: via}


def optionals_for_viewer(optionals: Any, *, sender_id: Any, viewer_id: Any) -> Any:
    """`item_optionals` as `viewer_id` may read it: its `via` through `via_for_viewer` (the sender,
    the member whose app it was, reads it whole; anyone else its kind and, for a connected app, the
    official family). A `via` that is not a usable one is left out. An item with no `via` is
    returned as it is; the input is never edited."""
    if not isinstance(optionals, dict) or KEY not in optionals:
        return optionals
    shown = via_for_viewer(optionals[KEY], sender_id, viewer_id)
    rest = {k: v for k, v in optionals.items() if k != KEY}
    return {**rest, KEY: shown} if shown else rest


def without_via(optionals: Any) -> Any:
    """What a client sent as `item_optionals`, minus any `via` it claimed for itself."""
    if not isinstance(optionals, dict) or KEY not in optionals:
        return optionals
    return {k: v for k, v in optionals.items() if k != KEY}
