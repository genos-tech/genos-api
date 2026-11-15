"""The visible mention tokens inside the member's own question (MIP-1585).

The Ask client writes a token into the question for every mention the member picked: `#label ` for a task, a
note, a chat, a project, a to-do, a calendar event or a message, `@label ` for a person or a group (the picker's
rule, `triggerForKind` in the frontend), and sends the same `label` with the entry of `mentions`. The label is
text somebody else chose: a task's or a note's title, a person's username, the sender of a message and, for a
calendar event, up to 60 characters of its TITLE, which anyone who can invite the member controls. The server
reads what a mention points at itself and fences what it reads, but the token sat in the user turn, outside every
fence, and a hostile title arrived with the trust of the member's own words.

**What this does.** While `AGENT_PARITY_TOOLS` is on, each token the member's question holds is replaced, before
any model sees the question, by a bracketed id the server made: `[task:123]`, `[message:<uuid>]`,
`[calendar_event:<account>:<event>]`. The references block names each item by the same bracket and carries no title
(`mentions.build_mention_system_extra`), so nothing a person chose reaches the prompt through a mention. A mention
that could not be used (missing, unreadable, deleted, malformed, past a limit) becomes `[reference not available]`,
the same words for every cause, so the question says nothing about which. The rest of the question is the member's
and is never touched.

**Not an either-or.** Fencing the label in the question would leave a fence in the middle of a sentence and still
put the text in the user turn; the references block already names the item by id. So the token is replaced, and the
block stopped repeating titles (they are the same text, in system-level words, replayed on a resumed run as sent).

**Finding the token.** The client keeps a mention only when its token is found in the final text, longest label
first, starting the text or following whitespace and not running into a longer word (`matchMentionTokens`). The
server looks for `<trigger><label>` with those two conditions made no stricter than the client's (a token
preceded by anything but an ASCII letter, digit or underscore; not followed by one: the client's `\\w` is ASCII),
so every token the client could have matched is found, longest first, each span claimed once. The label is used
as sent, and trimmed (the entry parser trims it). If the member EDITED the token after picking it the client no
longer sends the mention, nothing here matches and nothing is replaced: what remains is the member's own words.
A kind this server does not know is looked for under both triggers.

**Two audiences for `AgentRun.query`.** The member sees it (history, restore, "Ask again") and must keep seeing
what they typed; the model reads it again on the leg after an approval (`_rebuild_messages`) and as an earlier turn
of every later ask of the session (`_turns_of`). So the question the model reads is kept beside it in
`AgentRun.model_query`, written only while the flag is on and only when something was replaced, and read back
(`model_query_of`) only while the flag is on: a rollback of the flag brings back exactly what was there before.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from origin.search_engine.agent.mentions import (
    _VALID_TYPES,
    ResolvedMention,
    _identity_key,
    _normalise_entry,
    reference_id,
)

#: What a mention that could not be used becomes in the question: one phrase for every cause.
UNAVAILABLE = "[reference not available]"

# Kinds the picker opens with `@`; every other kind opens with `#` (`triggerForKind`).
_AT_KINDS = frozenset({"user", "group"})


def _is_word(ch: str) -> bool:
    """The client's `\\w`: ASCII letters, digits and the underscore, and nothing else."""
    return ch.isascii() and (ch.isalnum() or ch == "_")


def _triggers(kind: Any) -> tuple[str, ...]:
    if kind in _AT_KINDS:
        return ("@",)
    if kind in _VALID_TYPES:
        return ("#",)
    return ("#", "@")  # a kind this server does not know: either may have been written


def placeholder_for(m: ResolvedMention | None) -> str:
    """What a token stands for in the question: the item's bracketed id, or `UNAVAILABLE`."""
    ident = reference_id(m) if m is not None else None
    return f"[{ident}]" if ident else UNAVAILABLE


def slots_for_request(
    raw_mentions: Any, resolved: Sequence[ResolvedMention]
) -> list[tuple[str, str]]:
    """`(token text, placeholder)` for every entry of the request's `mentions` that carries a label, whether or
    not the mention was used. An entry is matched to its resolved mention by identity, so a duplicate, a fourth
    message past the limit, a message that is not readable and an entry that is malformed all come out as
    `UNAVAILABLE`, and a blank label (which would match every trigger) is skipped."""
    by_key: dict[tuple, ResolvedMention] = {}
    for m in resolved:
        if m.asked is not None:
            by_key.setdefault(_identity_key(m.asked), m)
    slots: list[tuple[str, str]] = []
    for raw in raw_mentions if isinstance(raw_mentions, list) else ():
        if not isinstance(raw, dict):
            continue
        label = raw.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        parsed = _normalise_entry(raw)
        match = by_key.get(_identity_key(parsed)) if parsed is not None else None
        holder = placeholder_for(match)
        for trigger in _triggers(raw.get("type")):
            for text in dict.fromkeys((label, label.strip())):
                slots.append((trigger + text, holder))
    return slots


def slots_for_resolved(resolved: Sequence[ResolvedMention]) -> list[tuple[str, str]]:
    """The same for mentions that are all resolved already (the eval runner builds them from a case)."""
    slots: list[tuple[str, str]] = []
    for m in resolved:
        entry = m.asked or {}
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        for trigger in _triggers(entry.get("type")):
            for text in dict.fromkeys((label, label.strip())):
                slots.append((trigger + text, placeholder_for(m)))
    return slots


def replace_tokens(query: str, slots: Sequence[tuple[str, str]]) -> str:
    """`query` with every token of `slots` replaced, longest first, each span claimed once. Linear in the length
    of the query for each distinct token (a marker array, not a scan of the spans found so far)."""
    if not slots or not query:
        return query
    # One entry per token text, holding the holders of the mentions that share it (two tasks with the same title, a label
    # sent twice or sent and trimmed to itself): the first occurrence goes to the first holder, the next to the second,
    # as the picker pairs a token with a mention, and any further occurrence to the last. So a request that repeats a
    # label does not repeat the scan.
    holders: dict[str, list[str]] = {}
    for token, holder in slots:
        held = holders.setdefault(token, [])
        if holder not in held:
            held.append(holder)
    claimed = bytearray(len(query))
    spans: list[tuple[int, int, str]] = []
    for token, held in sorted(holders.items(), key=lambda s: -len(s[0])):
        start = 0
        nth = 0
        while (idx := query.find(token, start)) != -1:
            end = idx + len(token)
            start = idx + 1
            if idx > 0 and _is_word(query[idx - 1]):
                continue
            if end < len(query) and _is_word(query[end]):
                continue
            if claimed.find(1, idx, end) != -1:
                continue
            claimed[idx:end] = b"\x01" * (end - idx)
            spans.append((idx, end, held[min(nth, len(held) - 1)]))
            nth += 1
    if not spans:
        return query
    out: list[str] = []
    at = 0
    for idx, end, holder in sorted(spans):
        out.append(query[at:idx])
        out.append(holder)
        at = end
    out.append(query[at:])
    return "".join(out)


def neutral_query(query: str, raw_mentions: Any, resolved: Sequence[ResolvedMention]) -> str:
    """The question the model reads: `query` with the tokens of the ask's mentions replaced (see the module
    docstring). `query` itself when nothing matched."""
    return replace_tokens(query, slots_for_request(raw_mentions, resolved))


def model_query_of(run: Any) -> str:
    """The question of a stored run as a model reads it: `model_query` while the flag is on and one was kept,
    else what the member typed (`query`). Flag off: exactly `query`, whatever a row holds."""
    from origin.search_engine.agent.parity import parity_on  # noqa: PLC0415

    kept = getattr(run, "model_query", None)
    if kept and parity_on():
        return kept
    return run.query
