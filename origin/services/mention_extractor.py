"""
Pure-Python BlockNote mention extractor.

Walks a BlockNote document body and pulls out the user / group ids that
appear in `mention` / `mentionGroup` inline-content nodes. The Django
message-create path uses this to populate the `MessageMention` table so
the `mentions[]` field on the serializer is non-empty.

Without this, the FE's `@you` indicator (which checks
`message.mentions[].mentionedUserId` against the viewer's id) never
fires for real messages — only for test fixtures that hand-craft
the array.

Body shape (matches the BlockNote spec consumed by `MessageBody.tsx`
on the FE):

    [
      {
        "type": "paragraph",
        "content": [
          {"type": "text", "text": "hello "},
          {"type": "mention", "props": {"userId": "<uuid>", "userName": "Alice"}},
          {"type": "text", "text": " and "},
          {"type": "mentionGroup", "props": {"groupId": "12", "groupName": "design"}}
        ]
      },
      ...
    ]

The functions are pure and parser-only — no DB writes, no Django
imports. They're trivially unit-testable on the raw dict input.

This module is intentionally separate from the legacy
`backend/utils/mention_handler.py` (which lives in the Flask repo and
mixes extraction with synchronous HTTP resolver calls). Duplicating the
walk logic keeps Django free of the cross-service HTTP dependency, at
the cost of one (small) extra file to maintain.
"""

from __future__ import annotations

from typing import Iterable, Set

# ---- Direct user mentions -------------------------------------------------


def extract_mentioned_user_ids(body) -> Set[str]:
    """Return the set of user UUIDs referenced by `mention` nodes.

    Accepts a list of BlockNote blocks (or any iterable; non-iterable
    input collapses to an empty set so a malformed payload doesn't
    crash the message-create transaction).
    """
    return _walk_for_props(body, node_type="mention", prop_key="userId")


# ---- Group mentions -------------------------------------------------------


def extract_mention_group_ids(body) -> Set[str]:
    """Return the set of mention-group ids referenced by `mentionGroup`
    nodes. Always coerced to `str` because the FE sometimes sends them
    as numbers (BigAutoField PK) and sometimes as strings — the consumer
    can re-parse to int if its model needs that.
    """
    return _walk_for_props(body, node_type="mentionGroup", prop_key="groupId")


# ---- Internals ------------------------------------------------------------


def _walk_for_props(body, *, node_type: str, prop_key: str) -> Set[str]:
    """Walk a BlockNote tree and collect `node.props[prop_key]` for
    every inline-content node whose `type == node_type`.

    Recurses through `content` arrays AND `children` arrays (BlockNote
    nests headings/lists/quotes as `children`). Non-dict items, missing
    `props`, and `None`/`""` prop values are silently skipped.
    """
    found: Set[str] = set()
    _walk(body, node_type, prop_key, found)
    return found


def _walk(node_or_list, node_type: str, prop_key: str, sink: Set[str]) -> None:
    """Recursive worker. Pulled out so the public functions stay
    parameter-light."""
    if isinstance(node_or_list, list):
        for item in node_or_list:
            _walk(item, node_type, prop_key, sink)
        return
    if not isinstance(node_or_list, dict):
        return

    # Inline-content node check.
    if node_or_list.get("type") == node_type:
        props = node_or_list.get("props")
        if isinstance(props, dict):
            value = props.get(prop_key)
            if value not in (None, ""):
                sink.add(str(value))

    # Recurse into `content` (inline-content array) and `children`
    # (nested blocks). Use `.get(...)` so missing keys are skipped
    # cheaply.
    inner_content = node_or_list.get("content")
    if isinstance(inner_content, (list, tuple)):
        for c in inner_content:
            _walk(c, node_type, prop_key, sink)

    children = node_or_list.get("children")
    if isinstance(children, (list, tuple)):
        for c in children:
            _walk(c, node_type, prop_key, sink)


# ---- Convenience for callers that want both in one pass -------------------


def extract_all_mentions(body) -> tuple[Set[str], Set[str]]:
    """Return `(user_ids, group_ids)` in one walk. Cheaper than calling
    `extract_mentioned_user_ids` + `extract_mention_group_ids` separately
    when the caller wants both — saves a second tree traversal.
    """
    users: Set[str] = set()
    groups: Set[str] = set()
    _walk_two(body, users, groups)
    return users, groups


def _walk_two(node_or_list, users: Set[str], groups: Set[str]) -> None:
    """Dual-collector variant of `_walk`. Same shape, two sinks."""
    if isinstance(node_or_list, list):
        for item in node_or_list:
            _walk_two(item, users, groups)
        return
    if not isinstance(node_or_list, dict):
        return

    ntype = node_or_list.get("type")
    if ntype == "mention":
        uid = (node_or_list.get("props") or {}).get("userId")
        if uid not in (None, ""):
            users.add(str(uid))
    elif ntype == "mentionGroup":
        gid = (node_or_list.get("props") or {}).get("groupId")
        if gid not in (None, ""):
            groups.add(str(gid))

    inner_content = node_or_list.get("content")
    if isinstance(inner_content, (list, tuple)):
        for c in inner_content:
            _walk_two(c, users, groups)
    children = node_or_list.get("children")
    if isinstance(children, (list, tuple)):
        for c in children:
            _walk_two(c, users, groups)


def _consume(iterable: Iterable) -> None:
    """Eat an iterator. Used in tests to force-evaluate a generator
    expression when we want to assert on its side effects only."""
    for _ in iterable:
        pass
