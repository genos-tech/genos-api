"""`list_my_mentions` tool — the caller's recent @mentions in chat.

Answers "who @mentioned me?" / "am I tagged anywhere?" by reading the v3
`MessageMention` table (origin/models/chat/unified_models.py), which is
populated on the live message send/edit path
(origin/views/chat/message_views.py). Rows come back newest-first with the
message text, sender, and the chat/thread coordinates the controller turns
into clickable Spotlight chips (controller.py `if call_name ==
"list_my_mentions"`).

This tool was previously a hard-coded empty stub, deferred on an
"OpenSearch indexer rewrite". That was a false blocker: reading a user's
mentions is a plain indexed ORM lookup — the model even ships a
purpose-built `(mentioned_user, ts_created_at)` index ("all mentions of
me, newest first") — and is independent of the semantic-search chunker.
Wired per GENOS_CAPABILITY_ROADMAP §3.2 so the prompt-advertised path
returns real results instead of a confidently wrong "nobody mentioned you".

Scope (documented so the model doesn't over-claim):
  * DIRECT @user mentions on CHAT messages (DM/GM/PM/MDM) only.
  * Group @-mentions (e.g. @engineering) are NOT expanded into rows yet
    (deferred at message_views.py:445-451), and task-comment / note
    mentions live on the Activity feed, not `MessageMention` — a broader
    "tagged anywhere" surface is a tracked follow-up.

ACL contract:
  * Tenant guard: only mentions in `ctx.team_id` channels.
  * Scope: `mentioned_user_id == ctx.user_id` (server-trusted) — no way
    for the LLM to read another user's mentions.
  * Live membership: only mentions in chats the user is CURRENTLY a member
    of (ChannelMember for DM/GM/MDM; ProjectMembers for PM), mirroring
    `chat_acl_user_ids` — a mention in a channel the user has since left is
    not surfaced.
  * Soft-deleted messages / channels are excluded.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from origin.models.chat.unified_models import ChannelMember, MessageMention
from origin.models.project.prj_models import ProjectMembers
from origin.search_engine.agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    wrap_workspace_content,
)
from origin.search_engine.chunkers.base import CHAT_TYPE_LABEL
from origin.search_engine.text_extraction import extract_text

_MAX_LIMIT = 50
_DEFAULT_LIMIT = 20


def _parse_since(raw: Any) -> datetime | None:
    """ISO 8601 datetime/date → aware datetime, or None if not supplied.

    Accepts a full datetime ("2026-08-01T09:30:00Z") or a bare date
    ("2026-08-01", taken as the start of that day). Raises `ToolError` on
    a non-empty but unparseable value so the model sees the mistake rather
    than silently getting an unfiltered list.

    Both Django parsers raise `ValueError` — not just return None — on a
    well-formed but impossible date like "2026-02-30", so the calls are
    wrapped: an escaping ValueError reaches `_run_tool_guarded`, which
    tags it "crash" and hands the model `"Internal error in tool
    'list_my_mentions'."` plus an ERROR-level traceback. That is the one
    thing this function exists to prevent — the model can self-correct
    from "must be ISO 8601", but not from an internal error. Every sibling
    parser (`get_my_schedule._parse_iso`, `create_todo_item._parse_date`,
    `create_task_plan._parse_date`) already converts ValueError this way.

    Note `parse_datetime` accepts a bare date on its own in Django 5.x, so
    the `parse_date` fallback is reached only by forms it rejects — chiefly
    the non-zero-padded "2026-9-28", which the model does emit.
    """
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    try:
        dt = parse_datetime(text)
        if dt is None:
            d = parse_date(text)
            if d is not None:
                dt = datetime.combine(d, time.min)
    except ValueError as e:
        raise ToolError(
            f"`since` must be an ISO 8601 datetime or date (got {raw!r}): {e}."
        ) from e
    if dt is None:
        raise ToolError(f"`since` must be an ISO 8601 datetime or date (got {raw!r}).")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        limit = int(args.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError):
        raise ToolError(f"`limit` must be an integer (got {args.get('limit')!r}).")
    limit = max(1, min(limit, _MAX_LIMIT))

    since_dt = _parse_since(args.get("since"))

    # Live ACL, computed once and applied in-SQL (the me-scoped pattern —
    # cf. list_my_milestones). A mention is surfaced only if the user is
    # CURRENTLY a member of its chat: DM/GM/MDM via ChannelMember, PM via
    # the channel's project's ProjectMembers. Union of the two mirrors
    # `chat_acl_user_ids`; a mention in a channel the user has left drops.
    member_channel_ids = set(
        ChannelMember.objects.filter(
            user_id=ctx.user_id,
            is_deleted=False,
            channel__team_id=ctx.team_id,
        ).values_list("channel_id", flat=True)
    )
    member_project_ids = set(
        ProjectMembers.objects.filter(
            attendee_id=ctx.user_id,
            project__team_id=ctx.team_id,
        ).values_list("project_id", flat=True)
    )
    visible = Q(message__channel_id__in=member_channel_ids) | Q(
        message__channel__project_id__in=member_project_ids
    )

    qs = (
        MessageMention.objects.select_related("message", "message__channel", "message__sender")
        .filter(
            mentioned_user_id=ctx.user_id,
            message__channel__team_id=ctx.team_id,
            message__deleted_at__isnull=True,
            message__channel__is_deleted=False,
        )
        .filter(visible)
    )
    if since_dt is not None:
        qs = qs.filter(ts_created_at__gte=since_dt)
    qs = qs.order_by("-ts_created_at")[:limit]

    mentions: list[dict[str, Any]] = []
    for row in qs:
        msg = row.message
        channel = msg.channel
        label = CHAT_TYPE_LABEL.get(channel.kind)
        if not label:
            continue
        # The message body is user-authored → wrap as untrusted data
        # (prompt-injection defence). extract_text may be "" for a
        # mention-only message; keep the row anyway — being tagged is the
        # signal, not the surrounding prose.
        text = extract_text(msg.body)
        mentions.append(
            {
                "message_id": str(msg.id),
                # chat_type_label / chat_id / thread_id / is_thread are the
                # keys controller.py reads to build the clickable chat chip.
                "chat_type_label": label,
                "chat_id": str(channel.id),
                "thread_id": str(msg.thread_root_id) if msg.thread_root_id else None,
                "is_thread": msg.is_thread_reply,
                "channel_title": channel.title or None,
                "sender_id": str(msg.sender_id) if msg.sender_id else None,
                "sender_username": msg.sender.username if msg.sender_id else None,
                "text": wrap_workspace_content(text),
                "ts": msg.ts_sent_at.isoformat() if msg.ts_sent_at else None,
            }
        )

    if mentions:
        latest = mentions[0]
        who = latest["sender_username"] or "someone"
        where = latest["channel_title"] or latest["chat_type_label"]
        summary = f"{len(mentions)} recent mention(s); latest from @{who} in {where}."
    else:
        summary = "No recent @mentions."

    return {
        "since": since_dt.isoformat() if since_dt else None,
        "mentions": mentions,
        "__summary__": summary,
    }


LIST_MY_MENTIONS = Tool(
    name="list_my_mentions",
    description=(
        "Recent @mentions of the current user in chat. Use for "
        "'who @mentioned me?', 'am I tagged anywhere?', 'any mentions for "
        "me?'. Returns chat messages where the caller was directly "
        "@-mentioned (DM/GM/PM/MDM), newest-first, each with the sender, "
        "text, and a link into the chat/thread. Direct @you mentions only "
        "— group @-mentions (e.g. @engineering) and task/note comment "
        "mentions are not covered yet. Caller's own mentions only; there "
        "is no way to specify another user."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "since": {
                "type": "STRING",
                "description": (
                    "Optional ISO 8601 datetime or date. Only mentions at "
                    "or after this time are returned."
                ),
            },
            "limit": {
                "type": "INTEGER",
                "description": (f"Max mentions to return (1–{_MAX_LIMIT}). Default {_DEFAULT_LIMIT}."),
            },
        },
        "required": [],
    },
    run=_run,
)
