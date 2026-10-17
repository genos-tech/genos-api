"""Turn a GitHub failure the caller can act on into a `ToolError`.

The five PR tools call the live GitHub API with the key owner's own OAuth
token. Two failures there are not bugs in Genos, and both used to escape
the tools as raw exceptions:

* **The grant is dead** (`ReauthRequired`): the refresh token was revoked
  or expired. Only the user can fix it, by reconnecting GitHub.
* **GitHub is unreachable** (`requests.RequestException`): a timeout or a
  connection error.

Uncaught, either one reached the caller as "failed unexpectedly. This is
a bug in Genos" over MCP, and as an opaque tool crash in the app. As a
`ToolError` it is a tool result the model can read and relay. Neither one
may read as an empty list: "no reviews" is a claim about the PR, and the
honest answer here is "couldn't look".
"""

from __future__ import annotations

from contextlib import contextmanager

import requests

from origin.search_engine.agent.tools.base import ToolError
from origin.services.oauth.tokens import ReauthRequired


@contextmanager
def github_errors_as_tool_errors():
    try:
        yield
    except ReauthRequired:
        raise ToolError(
            "The GitHub connection for this user has expired or was revoked. "
            "Reconnect GitHub in Genos under Integrations, then retry."
        ) from None
    except requests.RequestException as exc:
        raise ToolError(
            f"Couldn't reach GitHub ({type(exc).__name__}). This says nothing "
            "about the PR itself. Retry in a moment."
        ) from None
