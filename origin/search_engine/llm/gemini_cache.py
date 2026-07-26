"""Explicit Gemini context caching for the agent's static prefix.

Implicit caching is best-effort, and the 2026-07-26 per-call ledger
reads measured just how best-effort: ~half of medium-effort loop calls
paid full price for a byte-identical prefix — three consecutive
full-price misses inside one request, a first call of a fresh request
hitting at 83%, no pattern. At ~14-17K tokens of system prompt + 57
tool declarations re-sent on every step, a missed prefix costs ~3x a
hit call, and those misses were roughly HALF of medium's request cost.

An explicit `CachedContent` removes the luck: the prefix (system
instruction + tool declarations) is pinned server-side once, and every
loop call that references it bills the prefix at the cached rate,
guaranteed. Entries are keyed by a digest of (model, system, tools), so

  * all steps of one run share one entry, and
  * all requests whose system prompt is identical (the common case —
    `system_extra` is empty on a plain ask) share it too, across
    workers' independent maps at worst duplicating a cheap create.

FAIL-OPEN EVERYWHERE. Any error — create failed, SDK missing the API,
expired server-side — returns None and the caller sends the full
prefix exactly as today, with implicit caching still in play. A caching
layer must never be able to break generation.

Why the local expiry runs at 80% of the server TTL: a name that expires
server-side mid-loop fails the *next* generate call. Refreshing early
means the map never hands out a name in its last stretch of life; the
`forget()` hook covers the residual race by dropping an entry whose
generate failed, so the step after recreates instead of failing again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from origin.search_engine.llm import spend
from origin.search_engine.llm.types import CallUsage, ToolDeclaration

log = logging.getLogger(__name__)

# Refresh entries at 80% of the server TTL (see module docstring).
_TTL_MARGIN = 0.8
# Distinct prefixes worth remembering per worker. Prefix variants come
# from `system_extra` (thread/note summaries, mention blocks), so a
# handful exist at a time; 32 is generous and bounds the map.
_MAX_ENTRIES = 32

# The server refuses tiny caches (and they would save nothing anyway).
# The agent prefix is ~50K+ chars; this floor just keeps subprocess-
# sized prompts from ever being cached by mistake.
_MIN_PREFIX_CHARS = 8_000


@dataclass
class _Entry:
    name: str
    expires_at: float  # time.monotonic() deadline, already margin-scaled


_entries: OrderedDict[str, _Entry] = OrderedDict()
_lock = threading.Lock()


def _digest(model: str, system_instruction: str, tools: list[ToolDeclaration]) -> str:
    """Content digest over everything the cache would pin.

    Built from OUR neutral `ToolDeclaration`s, not the SDK objects —
    the SDK's repr is not a stability contract, and the declarations
    are already the canonical form the adapter translates from.
    """
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(b"\x00")
    h.update(system_instruction.encode())
    for t in tools:
        h.update(b"\x00")
        h.update(t.name.encode())
        h.update(b"\x00")
        h.update(t.description.encode())
        h.update(json.dumps(t.parameters_schema, sort_keys=True).encode())
    return h.hexdigest()


def prefix_cache_name(
    *,
    client: Any,
    model: str,
    system_instruction: str,
    tools: list[ToolDeclaration],
    sdk_tools: Any,
) -> str | None:
    """The `cached_content` resource name for this prefix, or None.

    None means "send the full prefix" — flag handling lives in the
    caller; this returns None only on smallness or failure.
    """
    if not tools or not system_instruction:
        return None
    approx_chars = len(system_instruction) + sum(
        len(t.name) + len(t.description) + len(str(t.parameters_schema)) for t in tools
    )
    if approx_chars < _MIN_PREFIX_CHARS:
        return None

    key = _digest(model, system_instruction, tools)
    now = time.monotonic()
    with _lock:
        entry = _entries.get(key)
        if entry is not None and entry.expires_at > now:
            _entries.move_to_end(key)
            return entry.name
        # Expired (or absent): drop it now so a concurrent failure
        # can't serve a dead name while we recreate below.
        _entries.pop(key, None)

    ttl_s = int(settings.SEARCH_ENGINE.get("GEMINI_EXPLICIT_CACHE_TTL_S", 3600))
    try:
        from google.genai import types  # noqa: PLC0415

        created = client.caches.create(
            model=model,
            config=types.CreateCachedContentConfig(
                system_instruction=system_instruction,
                tools=sdk_tools,
                ttl=f"{ttl_s}s",
                display_name=f"genos-prefix-{key[:16]}",
            ),
        )
    except Exception:  # noqa: BLE001 — fail open, always
        log.warning("Explicit prefix cache create failed; sending full prefix", exc_info=True)
        return None

    name = getattr(created, "name", None) or None
    if not name:
        return None

    _record_create(created, model)
    with _lock:
        _entries[key] = _Entry(name=name, expires_at=now + ttl_s * _TTL_MARGIN)
        while len(_entries) > _MAX_ENTRIES:
            _entries.popitem(last=False)
    return name


def forget(name: str) -> None:
    """Drop the entry serving `name` (a generate call using it failed).

    The failed step is already lost — this exists so the NEXT step
    recreates the cache instead of failing on the same dead name for
    the rest of the loop.
    """
    with _lock:
        for key, entry in list(_entries.items()):
            if entry.name == name:
                _entries.pop(key, None)


def _record_create(created: Any, model: str) -> None:
    """Ledger row for the create call.

    The cached tokens are billed once at creation (plus per-hour
    storage the meter deliberately does not model — a ~15K-token cache
    held for an hour is fractions of a cent). `cache_write_tokens` is
    the bucket that already exists for exactly this concept.
    """
    try:
        usage = getattr(created, "usage_metadata", None)
        total = int(getattr(usage, "total_token_count", None) or 0)
        cu = CallUsage(provider="gemini", model=model)
        cu.cache_write_tokens = total
        cu.total_tokens = total
        spend.record_llm_call(cu)
    except Exception:  # noqa: BLE001 — accounting never breaks generation
        log.debug("Explicit cache create accounting failed", exc_info=True)


def _reset_for_tests() -> None:
    with _lock:
        _entries.clear()
