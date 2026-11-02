"""`list_my_chats` tool — the caller's own chats, to find one by name.

Neither the in-app agent nor MCP could find a chat by its name: a `chat_id`
had to come from a search hit or a mention. This lists them, and each row
carries the `chat_type` and `chat_id` that `fetch_chat_thread` takes.

Which chats count is NOT decided here. It is the app's own chat list, the
scoping `ChannelListView.get` uses (`services/channel_scope.py`): direct
messages, groups, multi-person DMs and project chats the caller holds an
active membership on, narrowed to this team's list, which includes chats
another team shared with it and the project chat of a project shared with it.
A chat the caller left, or never joined, is not in it.

ACL contract:
  * Scope: the caller's memberships (`ctx.user_id`, server-trusted), and
    `ctx.team_id`'s chat list. Nothing here takes a user or a team from the model.
  * Names: a group's or project's title, and for a direct message the other
    person's username, which the app also derives (a DM has no title of its
    own). All of it is user-typed, and rides under `title`, which the
    controller's model boundary fences.

Paged like `list_tasks`: most recently active first, a hard cap per page, and
an opaque `next_cursor` that is a total order (activity time, then id).
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime
from typing import Any

from django.db.models import Count, Exists, F, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce

from origin.models.chat.unified_models import ChannelKind, ChannelMember, Message
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.chunkers.base import CHAT_TYPE_LABEL
from origin.services.channel_scope import team_scope_q, user_channels_qs

_MAX_LIMIT = 50
_DEFAULT_LIMIT = 20
# A name filter is a word or two; the rest is not worth a LIKE over.
_MAX_QUERY_CHARS = 100
# How many other people's names a multi-person DM's title lists.
_MDM_NAMES = 5

_LABEL_TO_CODE: dict[str, int] = {v: k for k, v in CHAT_TYPE_LABEL.items()}


def _encode_cursor(last_at: datetime | None, channel_id: uuid.UUID) -> str:
    """Opaque position of the last row of a page: where the next one starts."""
    raw = json.dumps(
        {"t": last_at.isoformat() if last_at else None, "id": str(channel_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime | None, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        at = datetime.fromisoformat(data["t"]) if data["t"] else None
        return at, str(uuid.UUID(data["id"]))
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise ToolError(
            "`cursor` is not valid. Pass back the `next_cursor` of the previous page "
            "unchanged, together with the same filters."
        )


def _name_filter(query: str, user_id: str) -> Q:
    """Chats whose NAME contains `query`, as the app names them: a title for a
    group or project chat, the other people for a direct message. In SQL, so a
    page boundary falls on matching rows only and `has_more` stays true."""
    others = ChannelMember.objects.filter(channel=OuterRef("pk"), is_deleted=False).exclude(
        user_id=user_id
    )
    match = Q(title__icontains=query) | (
        Q(kind__in=[ChannelKind.DM, ChannelKind.MDM])
        & Exists(others.filter(user__username__icontains=query))
    )
    # A chat with yourself is named after you (the app shows "(you)").
    own_name = CustomUser.objects.filter(id=user_id).values_list("username", flat=True).first()
    if own_name and query.lower() in own_name.lower():
        match |= Q(kind=ChannelKind.DM) & ~Exists(others)
    return match


def _titles(channels: list, user_id: str) -> dict[Any, str]:
    """Readable name per channel id. A stored title wins; a DM or multi-person
    DM has none and is named after the other people, as the app does."""
    unnamed = [
        c.id for c in channels if not c.title and c.kind in (ChannelKind.DM, ChannelKind.MDM)
    ]
    people: dict[Any, list[tuple[Any, str]]] = {}
    if unnamed:
        for m in (
            ChannelMember.objects.filter(channel_id__in=unnamed, is_deleted=False)
            .select_related("user")
            .order_by("ts_joined_at")
        ):
            people.setdefault(m.channel_id, []).append((m.user_id, (m.user.username or "")))
    out: dict[Any, str] = {}
    for c in channels:
        if c.title:
            out[c.id] = c.title
            continue
        members = people.get(c.id, [])
        others = [name for uid, name in members if str(uid) != user_id and name]
        if c.kind == ChannelKind.DM:
            # The partner; in a chat with yourself, you.
            out[c.id] = others[0] if others else (members[0][1] if members else "")
        elif c.kind == ChannelKind.MDM:
            shown = ", ".join(others[:_MDM_NAMES])
            more = len(others) - _MDM_NAMES
            out[c.id] = f"{shown} +{more} more" if more > 0 else shown
        else:
            out[c.id] = ""
    return out


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # ---- arguments first ----
    raw_type = args.get("chat_type")
    chat_type = str(raw_type).lower().strip() if raw_type not in (None, "") else ""
    if chat_type and chat_type not in _LABEL_TO_CODE:
        raise ToolError(
            f"Unknown chat_type {chat_type!r}; expected one of {sorted(_LABEL_TO_CODE)}."
        )

    try:
        limit = int(args.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError):
        raise ToolError(f"`limit` must be an integer (got {args.get('limit')!r}).")
    limit = max(1, min(limit, _MAX_LIMIT))

    query = str(args.get("query") or "").strip()[:_MAX_QUERY_CHARS]
    after: tuple[datetime | None, str] | None = None
    if args.get("cursor"):
        after = _decode_cursor(str(args["cursor"]))

    # ---- the app's own chat list, for this caller and this team ----
    qs = user_channels_qs(ctx.user_id).select_related(None)
    qs = qs.filter(team_scope_q(ctx.team_id, ctx.user_id))
    if chat_type:
        qs = qs.filter(kind=_LABEL_TO_CODE[chat_type])
    if query:
        qs = qs.filter(_name_filter(query, ctx.user_id))

    last_message_at = (
        Message.objects.filter(
            channel=OuterRef("pk"), is_thread_reply=False, deleted_at__isnull=True
        )
        .order_by("-seq")
        .values("ts_sent_at")[:1]
    )
    member_count = (
        ChannelMember.objects.filter(channel=OuterRef("pk"), is_deleted=False)
        .order_by()
        .values("channel")
        .annotate(c=Count("pk"))
        .values("c")[:1]
    )
    qs = qs.annotate(
        _last_at=Subquery(last_message_at),
        _member_count=Coalesce(Subquery(member_count), 0),
    )

    # Most recently active first, id to break ties and to place the chats with
    # no message yet (last). A total order, so a page boundary falls between two
    # rows the same way every time; a chat that gets a message between two
    # requests moves to the front and is not seen again, as with `list_tasks`.
    qs = qs.order_by(F("_last_at").desc(nulls_last=True), "-id")
    if after is not None:
        at, after_id = after
        if at is None:
            qs = qs.filter(_last_at__isnull=True, id__lt=after_id)
        else:
            qs = qs.filter(
                Q(_last_at__lt=at) | Q(_last_at=at, id__lt=after_id) | Q(_last_at__isnull=True)
            )

    # One extra row says whether anything is left.
    rows = list(qs[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]

    titles = _titles(rows, ctx.user_id)
    chats = []
    for c in rows:
        row: dict[str, Any] = {
            "chat_type": CHAT_TYPE_LABEL.get(c.kind),
            "chat_id": str(c.id),
            "title": titles.get(c.id, ""),
            "member_count": c._member_count,
            "last_message_at": c._last_at.isoformat() if c._last_at else None,
        }
        if c.kind == ChannelKind.PM and c.project_id:
            row["project_id"] = c.project_id
        chats.append(row)

    result: dict[str, Any] = {"chats": chats, "has_more": has_more}
    summary = f"Found {len(chats)} chat(s)"
    if has_more:
        result["next_cursor"] = _encode_cursor(rows[-1]._last_at, rows[-1].id)
        summary += " (more available)"
    result["__summary__"] = summary
    return result


LIST_MY_CHATS = Tool(
    name="list_my_chats",
    description=(
        "The caller's own chats — direct messages, group chats, multi-person "
        "DMs and project chats they belong to — most recently active first. "
        "Use it to find a chat by name ('my chat with Alice', 'the design "
        "group') or to see what chats exist, then read one with "
        "`fetch_chat_thread` using the row's chat_type and chat_id. A direct "
        "message is named after the other person. Rows carry member_count and "
        "last_message_at (null for a chat with no message yet), and project_id "
        "for a project chat. At most 50 a page: when `has_more` is true the "
        "list is partial, and `next_cursor` (passed back as `cursor`, with "
        "the same filters) returns the next page. Same chats as the app's "
        "chat list for this team, including chats another team shared with "
        "it. Only the caller's own chats; there is no way to list another "
        "person's."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": (
                    "Optional. Only chats whose name contains this text "
                    "(case-insensitive): a group or project name, or the "
                    "other person's name for a direct message."
                ),
            },
            "chat_type": {
                "type": "STRING",
                "enum": ["dm", "gm", "mdm", "pm"],
                "description": (
                    "Optional. Only this kind of chat: dm (direct), gm (named "
                    "group), mdm (multi-person DM), pm (project chat)."
                ),
            },
            "limit": {
                "type": "INTEGER",
                "description": f"Page size (1–{_MAX_LIMIT}). Default {_DEFAULT_LIMIT}.",
            },
            "cursor": {
                "type": "STRING",
                "description": (
                    "The `next_cursor` of the previous page, to get the next one. "
                    "Use the same filters as that call."
                ),
            },
        },
        "required": [],
    },
    run=_run,
)
