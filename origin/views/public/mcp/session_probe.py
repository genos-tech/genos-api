"""Spike: does an MCP client send `Mcp-Session-Id` back? (MIP-2010), behind `MCP_SESSION_ID_PROBE`.

Why. The agent-call log ([MIP-2008]) wants a `run_id` per conversation. Without one it falls back to a
synthetic session (a new id after 20 quiet minutes per credential). If the clients that matter echo a
server-issued `Mcp-Session-Id`, that header is a better run id. This module finds out, and does nothing
else.

What, with the switch on:

* `initialize` (the legacy handshake) is answered with an `Mcp-Session-Id` header. The body is
  unchanged. The id is signed, not stored: `g1.<issued, base36 seconds>.<random>.<mac>.<client>`,
  where the mac is an HMAC over the rest and the credential that asked, so any worker can tell
  "an id we issued to this credential" from anything else without Redis or a table.
* Every MCP request is logged on one INFO line: whether it carried an id and what kind
  (`echoed` / `foreign` / `malformed` / `absent`), the id's age, a short tag to group one session's
  lines, the client's name (from the id, from the modern `_meta` clientInfo, or from `initialize`),
  the era, the JSON-RPC method and the first product token of the User-Agent.
* GET and DELETE (which this server answers with 405) are logged the same way before the 405, because
  a DELETE is a client ending its session.
* A call carrying ChatGPT's per-conversation id (`_meta["openai/session"]`, OpenAI Apps SDK) gets a
  hash tag of it on the line too, so the same hour also shows whether that is a usable run id.
  The NAMES of every `params._meta` key are logged too (never a value), to spot a conversation
  key some other client sends.
* Only a LEGACY `initialize` gets an id: `2026-07-28` removed sessions and says a server should not
  mint or echo one, so a modern-era client never sees it (and the probe learns nothing from it
  beyond its client name: that is a finding too).

What it never does: reject or change a call. A missing, unknown or foreign id is logged and the call
runs exactly as with the switch off. That breaks the letter of the 2025 transport spec (a server that
issues ids SHOULD answer a request without one with 400, and an unknown one with 404), deliberately:
this is a measurement, and a strict server would make clients that ignore the header stop working.

What it never logs: a user, team, credential, address, argument, result or the id itself (the tag is a
hash prefix of it). Client names and User-Agent tokens are reduced to `[A-Za-z0-9_./-]`, 40 characters.

The switch is read on every request, so flipping it needs no restart. Not in `apis/settings.py`: a
change to that file used to start the billed `agent-evals` workflow, and an env var is enough here.
Off (the default): nothing here runs past the switch check, and responses are byte-identical.

Nothing here may fail a call: `observe` and `observe_other_method` swallow their own errors.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from typing import Any

from django.conf import settings

log = logging.getLogger(__name__)

FLAG_ENV = "MCP_SESSION_ID_PROBE"
HEADER = "Mcp-Session-Id"
_TRUE = frozenset({"1", "true", "yes", "on"})

_PREFIX = "g1"
_SAFE = re.compile(r"[^A-Za-z0-9_./-]+")
_BASE36 = re.compile(r"[0-9a-z]{1,13}")
_RAND = re.compile(r"[A-Za-z0-9_-]{8,64}")
_HEX20 = re.compile(r"[0-9a-f]{20}")
_MAX_TOKEN = 40
# A header longer than this is not one of ours and is not worth hashing.
_MAX_ID_LEN = 200
_CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"
# ChatGPT's anonymized conversation id on every tool call (OpenAI Apps SDK).
_OPENAI_SESSION_META = "openai/session"
_MAX_META_KEYS = 10


def enabled() -> bool:
    """Is the probe on right now? Off unless the env var says so."""
    return os.environ.get(FLAG_ENV, "").strip().lower() in _TRUE


def _safe_token(value: Any, limit: int = _MAX_TOKEN) -> str:
    """A client name or UA token reduced to a log- and header-safe word, or `-`."""
    if not isinstance(value, str):
        return "-"
    cleaned = _SAFE.sub("", value)[:limit]
    return cleaned or "-"


def _credential_key(request) -> str:
    """What the id is bound to: the credential's kind and id. Never logged."""
    from origin.views.public.credentials import credential_of

    credential = credential_of(request)
    if credential is None:
        user = getattr(request, "user", None)
        return f"user:{getattr(user, 'pk', '')}"
    return f"{credential.kind}:{credential.id}"


def _key() -> bytes:
    return hashlib.sha256(f"mcp-session-probe|{settings.SECRET_KEY}".encode()).digest()


def _mac(payload: str, credential_key: str) -> str:
    message = f"{payload}|{credential_key}".encode()
    return hmac.new(_key(), message, hashlib.sha256).hexdigest()[:20]


def _tag(value: str) -> str:
    """A short keyed hash to group one id's log lines. Keyed, so a log reader holding a raw id
    (a session id, a ChatGPT conversation id) cannot look it up in the logs."""
    data = value[:_MAX_ID_LEN].encode("utf-8", "replace")
    return hmac.new(_key(), b"tag|" + data, hashlib.sha256).hexdigest()[:8]


def issue(request, client: str, *, now: float | None = None) -> str:
    """A new signed session id for this credential, naming the client. Visible ASCII only."""
    issued = int(now if now is not None else time.time())
    payload = f"{_PREFIX}.{_base36(issued)}.{secrets.token_urlsafe(16)}"
    slug = _safe_token(client)
    return f"{payload}.{_mac(payload + '.' + slug, _credential_key(request))}.{slug}"


def classify(request, value: str | None, *, now: float | None = None) -> dict[str, Any]:
    """What a presented `Mcp-Session-Id` is: `absent`, `malformed` (not shaped like ours),
    `foreign` (shaped like ours, but not issued to this credential), or `echoed`."""
    if not value:
        return {"sid": "absent"}
    value = value.strip()
    tag = _tag(value)
    if len(value) > _MAX_ID_LEN:
        return {"sid": "malformed", "tag": tag}
    parts = value.split(".", 4)
    if len(parts) != 5 or parts[0] != _PREFIX:
        return {"sid": "malformed", "tag": tag}
    _, issued36, rand, mac, slug = parts
    # Our alphabets only: also keeps non-ASCII away from `compare_digest`, which raises on it.
    if not (_BASE36.fullmatch(issued36) and _RAND.fullmatch(rand) and _HEX20.fullmatch(mac)):
        return {"sid": "malformed", "tag": tag}
    if _SAFE.search(slug) or not slug:
        return {"sid": "malformed", "tag": tag}
    issued = int(issued36, 36)
    payload = f"{_PREFIX}.{issued36}.{rand}.{slug}"
    if not hmac.compare_digest(mac, _mac(payload, _credential_key(request))):
        return {"sid": "foreign", "tag": tag}
    current = int(now if now is not None else time.time())
    return {"sid": "echoed", "tag": tag, "age_s": max(0, current - issued), "client": slug}


def _base36(number: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        number, rem = divmod(number, 36)
        out = digits[rem] + out
        if number == 0:
            return out


def _client_from_body(body: dict[str, Any]) -> str | None:
    """The client's name as it introduced itself: `initialize` clientInfo (legacy) or the
    per-request `_meta` clientInfo (modern)."""
    params = body.get("params")
    if not isinstance(params, dict):
        return None
    info = params.get("clientInfo")
    if not isinstance(info, dict):
        meta = params.get("_meta")
        info = meta.get(_CLIENT_INFO_META) if isinstance(meta, dict) else None
    if not isinstance(info, dict):
        return None
    name = info.get("name")
    version = info.get("version")
    if not isinstance(name, str):
        return None
    return f"{name}/{version}" if isinstance(version, str) and version else name


def _ua(request) -> str:
    """The first product token of the User-Agent (`claude-code/2.1.201`, `python-httpx/0.28`)."""
    raw = request.headers.get("User-Agent") or ""
    first = raw.split(" ", 1)[0] if raw else ""
    return _safe_token(first) if first else "-"


def _openai_session_tag(body: dict[str, Any]) -> str | None:
    """A hash tag of ChatGPT's per-conversation id, `_meta["openai/session"]` (Apps SDK), when the
    call carries one. The id itself is never logged."""
    params = body.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    value = meta.get(_OPENAI_SESSION_META) if isinstance(meta, dict) else None
    if not isinstance(value, str) or not value:
        return None
    return _tag(value)


def _meta_keys(body: dict[str, Any]) -> str | None:
    """The NAMES of the keys in `params._meta` (never their values), comma-joined, at most 10, so
    the log shows whether a client sends some conversation key we do not know about yet."""
    params = body.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict) or not meta:
        return None
    # 64, not 40: `io.modelcontextprotocol/clientCapabilities` is 42 characters.
    names = sorted(_safe_token(k, 64) for k in meta if isinstance(k, str))[:_MAX_META_KEYS]
    return ",".join(names) or None


def _line(
    request,
    *,
    http: str,
    method: str,
    era: str,
    seen: dict[str, Any],
    client,
    issued,
    oai_tag: str | None = None,
    meta_keys: str | None = None,
):
    parts = [
        "mcp.session_probe",
        f"http={http}",
        f"method={_safe_token(method)}",
        f"era={era}",
        f"sid={seen['sid']}",
    ]
    if "age_s" in seen:
        parts.append(f"age_s={seen['age_s']}")
    if "tag" in seen:
        parts.append(f"sid_tag={seen['tag']}")
    if issued:
        parts.append("issued=1")
    if oai_tag:
        parts.append(f"oai_session_tag={oai_tag}")
    if meta_keys:
        parts.append(f"meta_keys={meta_keys}")
    parts.append(f"client={_safe_token(client)}")
    parts.append(f"ua={_ua(request)}")
    log.info(" ".join(parts))


def observe(request, body: dict[str, Any], response) -> None:
    """With the probe on: issue an id on a successful `initialize`, and log this request.
    Called after the response is built; never raises and never changes the body or status."""
    try:
        if not enabled():
            return
        from origin.views.public.mcp import protocol

        modern = protocol.is_modern(body)
        method = body.get("method")
        seen = classify(request, request.headers.get(HEADER))
        client = _client_from_body(body) or seen.get("client")
        issued = False
        # Legacy `initialize` only: 2026-07-28 removed sessions and tells a server not to mint or
        # echo an id, so a modern response never carries one.
        if method == "initialize" and not modern and getattr(response, "status_code", None) == 200:
            response[HEADER] = issue(request, client or "-")
            issued = True
        _line(
            request,
            http="POST",
            method=method if isinstance(method, str) else "-",
            era="modern" if modern else "legacy",
            seen=seen,
            client=client,
            issued=issued,
            oai_tag=_openai_session_tag(body),
            meta_keys=_meta_keys(body),
        )
    except Exception:  # noqa: BLE001 - a probe must never fail a call
        log.debug("mcp.session_probe failed", exc_info=True)


def observe_other_method(request) -> None:
    """With the probe on: log a GET (stream) or DELETE (end of session) before its 405."""
    try:
        if not enabled():
            return
        seen = classify(request, request.headers.get(HEADER))
        _line(
            request,
            http=_safe_token(request.method),
            method="-",
            era="-",
            seen=seen,
            client=seen.get("client"),
            issued=False,
        )
    except Exception:  # noqa: BLE001 - a probe must never fail a call
        log.debug("mcp.session_probe failed", exc_info=True)
