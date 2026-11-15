"""Which official AI product a connected app is: `claude`, `chatgpt`, or none (MIP-1568).

Ken wants the official Claude / ChatGPT icon on what those connections did. The family is a
coarse, closed label; it is stamped on a connected app's `via` when it writes
(`McpOAuthCredential.via`), and `via_for_viewer` decides who may read it.

**What is trusted.** Registration is open (`services/mcp_oauth.py`): anyone may register a client
under any name, so the NAME is never read here. Two paths, by how the client came to be:

* A client registered by DCR (its `client_id` is a random string; the registration cannot be
  edited afterwards, no registration access token is issued). The signal is its REGISTERED
  REDIRECT URIs, and all of them: the family is earned only when every registered redirect URI
  is an official callback of that one family, compared exactly (no parsing, so no parser
  differential: a lookalike host, a userinfo trick, a port, a query, a trailing slash, an
  upper-case host all fail the comparison). A client that lists its own address next to an
  official one earns nothing. Why this is enough: the authorization code is sent to the registered
  redirect URI, so a client that registers only the official callbacks sends it to claude.ai /
  chatgpt.com and cannot read it; it never gets a token, so no write carries its stamp.
* A client identified by a metadata document (CIMD: its `client_id` IS an https URL). Its row is
  rebuilt from the document, and refreshed when the document's cache expires, so a stranger could
  first connect with a callback of its own and later publish an official-only document: the
  redirect URIs of such a row prove nothing. The signal is the `client_id` URL itself, compared
  exactly with the official metadata documents: only the owner of claude.ai / chatgpt.com can
  serve a document there. A CIMD client under any other URL earns nothing, whatever it lists.

**What is assumed** (Genos docs `operations/MCP_OAUTH_CLIENTS.md`, probe logs 2026-11-07):

* claude.ai (web, Desktop, mobile, Cowork) uses one fixed callback,
  `https://claude.ai/api/mcp/auth_callback` (observed). `https://claude.com/api/mcp/auth_callback`
  is ASSUMED to be its twin on Anthropic's other domain: it is in no doc or log of this repo, and
  is listed only because the domain is Anthropic's to serve.
* claude.ai connects with the metadata document `https://claude.ai/oauth/mcp-oauth-client-metadata`
  (observed in production); ChatGPT's is `https://chatgpt.com/oauth/client.json`, or
  `https://chatgpt.com/oauth/{callback_id}/client.json` (documented in RESEARCH.md, not observed).
* ChatGPT uses `https://chatgpt.com/connector_platform_oauth_redirect` (observed), or, for a
  server that does not meet its issuer-identification rule, `https://chatgpt.com/connector/oauth/
  {callback_id}` (documented; the id's character set is not stated, so a conservative one is
  accepted). OpenAI's developer-console callback and `chat.openai.com` are not listed on purpose.

Not covered, so no icon: Claude Code and other clients that use a loopback redirect (a loopback
address is claimable by anything on the member's machine), and any other client.
"""

from __future__ import annotations

import re
from typing import Any

APP_FAMILIES: tuple[str, ...] = ("claude", "chatgpt")

# Per family: callbacks compared as whole strings, and patterns that must match the whole string.
_CALLBACKS: dict[str, tuple[frozenset[str], tuple[re.Pattern[str], ...]]] = {
    "claude": (
        frozenset(
            {
                "https://claude.ai/api/mcp/auth_callback",
                "https://claude.com/api/mcp/auth_callback",
            }
        ),
        (),
    ),
    "chatgpt": (
        frozenset({"https://chatgpt.com/connector_platform_oauth_redirect"}),
        (re.compile(r"https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,128}"),),
    ),
}


def _is_callback_of(family: str, uri: str) -> bool:
    exact, patterns = _CALLBACKS[family]
    return uri in exact or any(p.fullmatch(uri) for p in patterns)


# The official metadata documents (CIMD client ids), whole-string comparisons.
_METADATA_DOCUMENTS: dict[str, tuple[frozenset[str], tuple[re.Pattern[str], ...]]] = {
    "claude": (frozenset({"https://claude.ai/oauth/mcp-oauth-client-metadata"}), ()),
    "chatgpt": (
        frozenset({"https://chatgpt.com/oauth/client.json"}),
        (re.compile(r"https://chatgpt\.com/oauth/[A-Za-z0-9_-]{1,128}/client\.json"),),
    ),
}


def _is_metadata_document_of(family: str, client_id: str) -> bool:
    exact, patterns = _METADATA_DOCUMENTS[family]
    return client_id in exact or any(p.fullmatch(client_id) for p in patterns)


def app_family(application: Any) -> str | None:
    """`claude` or `chatgpt` for an official client; otherwise None.

    A client whose `client_id` is a URL (CIMD) is judged by that URL alone; any other client by
    its registered redirect URIs (every one an official callback of one family). Never reads the
    client's name."""
    client_id = getattr(application, "client_id", None)
    if isinstance(client_id, str) and client_id[:8].lower() == "https://":
        return next(
            (f for f in APP_FAMILIES if _is_metadata_document_of(f, client_id)),
            None,
        )
    raw = getattr(application, "redirect_uris", None)
    if not isinstance(raw, str):
        return None
    uris = raw.split()
    if not uris:
        return None
    for family in APP_FAMILIES:
        if all(_is_callback_of(family, uri) for uri in uris):
            return family
    return None


def credential_family(credential: Any) -> str | None:
    """`claude` or `chatgpt` when `credential` is a connected app of that official family, else None.

    What `McpOAuthCredential.via` stamped from the client's registered redirect URIs (never its
    name), read from the credential in hand; an API key, any other kind of credential, one that
    cannot say, and a family that is not one of `APP_FAMILIES` all answer None."""
    if getattr(credential, "kind", None) != "oauth":
        return None
    try:
        via = credential.via()
    except Exception:  # noqa: BLE001 - a credential that cannot describe itself has no family
        return None
    app = via.get("app") if isinstance(via, dict) else None
    return app if isinstance(app, str) and app in APP_FAMILIES else None
