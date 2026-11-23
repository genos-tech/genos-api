"""Job `chat_catch_up` (MIP-2061): which group chats are busy now.

"Every morning: group chats catch-up". Data only: no model, no cost, nothing written, and no
message text is ever read: a chat is named with how many messages it got and how many people
wrote them, and links to the chat.

A chat is named only when the schedule's owner AND every other reader of the chat the report is
posted into (`AUDIENCE_KEY`) is in it, so a report in a chat never tells anyone about a chat they
are not in. Only live, private-or-member group chats of this team count: not archived, not shared
with another team, not a direct message. A message the report itself posted has no author and is
not counted.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Count
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.search_engine.agent.tools.base import ToolContext
from origin.services.channel_scope import chat_member_q
from origin.services.schedule_jobs import AUDIENCE_KEY, Job, Report, register_job
from origin.services.schedule_jobs.report_kit import Links, counted, item, more, span

DEFAULT_HOURS = 24
MAX_LIST = 8
MIN_BUSY = 3


def run(ctx: ToolContext, params: dict[str, Any]) -> Report:
    hours = int(params.get("hours", DEFAULT_HOURS))
    audience = {str(ctx.user_id), *(str(i) for i in params.get(AUDIENCE_KEY) or [])}
    since = timezone.now() - timedelta(hours=hours)

    # Group chats the owner is in, and that every other reader is in too (one query each).
    everyone = (
        ChannelMember.objects.filter(is_deleted=False, user_id__in=audience)
        .values("channel_id")
        .annotate(n=Count("user_id", distinct=True))
        .filter(n=len(audience))
        .values("channel_id")
    )
    chats = {
        c.id: c
        for c in Channel.objects.filter(
            chat_member_q(ctx.user_id),
            id__in=everyone,
            team_id=ctx.team_id,
            kind=ChannelKind.GM,
            is_deleted=False,
            archived_at__isnull=True,
            is_external=False,
        ).exclude(title="")
    }
    busy = []
    if chats:
        busy = list(
            Message.objects.filter(
                channel_id__in=chats,
                ts_sent_at__gte=since,
                deleted_at__isnull=True,
                sender__isnull=False,
            )
            .values("channel_id")
            .annotate(n=Count("id"), people=Count("sender_id", distinct=True))
            .filter(n__gte=MIN_BUSY)
            .order_by("-n", "channel_id")
        )
    rows = [
        item(
            f"{chats[b['channel_id']].title}: {b['n']} messages from {b['people']} "
            f"{'person' if b['people'] == 1 else 'people'}",
            Links.chat("gm", chats[b["channel_id"]].id),
            "chat",
        )
        for b in busy[:MAX_LIST]
    ]
    if len(busy) > len(rows):
        rows.append(more(len(busy) - len(rows), "chats"))
    return {
        "title": "Group chat catch-up",
        "summary": (
            f"{counted(len(busy), 'busy group chat')} in the last {span(hours)}."
            if busy
            else f"No group chat was busy in the last {span(hours)}."
        ),
        # The section's key is the headline chip's key, so the chip is named by the heading.
        "sections": [{"heading": "Busy group chats", "key": "chats", "items": rows}]
        if rows
        else [],
        "stats": [{"key": "chats", "count": len(busy)}] if busy else [],
        "quiet": not busy,
    }


register_job(
    Job(
        key="chat_catch_up",
        title="Group chat catch-up",
        description=(
            "Which group chats are busy now: how many messages and how many people in the last "
            "hours. Only chats you are in; in a chat, only chats everyone there is in. It never "
            "shows what was said."
        ),
        run=run,
        share_chats=True,
        params_schema={"properties": {"hours": {"type": "integer", "minimum": 1, "maximum": 168}}},
    )
)
