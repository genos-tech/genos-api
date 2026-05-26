"""`list_my_mentions` tool — recent `@mentions` of the caller.

Surfaces `MentionFact` rows where `mentioned_user_id == ctx.user_id`.
Each row points at a chat message via the (chat_type, chat_id,
thread_id, message_id) tuple. The agent can follow up with
`fetch_chat_thread` to read the message body.

Chat type codes (kept in sync with
`reference_chat_type_codes` memory and the frontend mappings):
  1 = DM   (1:1 direct message)
  2 = GM   (group message)
  3 = PM   (project message — the project's chat room)
  4 = MDM  (multi-DM)

ACL contract:
  * Tenant guard: ctx.team_id.
  * Scope: `mentioned_user_id == ctx.user_id` (server-trusted). The
    caller cannot view another user's mentions. We do NOT additionally
    redact by project membership because chat rooms can span projects
    and the mention is by definition addressed AT the caller.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from origin.models.chat.mention_models import MentionFact
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError

_MAX_LIMIT = 50
_DEFAULT_LIMIT = 20
_DEFAULT_SINCE_DAYS = 30

_CHAT_TYPE_LABELS = {
    1: "dm",
    2: "gm",
    3: "pm",
    4: "mdm",
}


def _parse_since(value: Any) -> datetime:
    if value is None:
        return timezone.now() - timedelta(days=_DEFAULT_SINCE_DAYS)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"`since` must be an ISO 8601 date/datetime (got {value!r}).")
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        raise ToolError(
            f"`since` must be ISO 8601 (e.g. '2026-04-27' or "
            f"'2026-04-27T00:00:00Z'); got {value!r}."
        )
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    since_dt = _parse_since(args.get("since"))

    try:
        limit = int(args.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError):
        raise ToolError(f"`limit` must be an integer (got {args.get('limit')!r}).")
    limit = max(1, min(limit, _MAX_LIMIT))

    qs = MentionFact.objects.filter(
        team_id=ctx.team_id,
        mentioned_user_id=ctx.user_id,
        ts_created_at__gte=since_dt,
    ).order_by("-ts_created_at")[:limit]

    mentions: list[dict[str, Any]] = []
    for row in qs:
        mentions.append(
            {
                "uid": row.uid,
                "chat_type": row.chat_type,
                "chat_type_label": _CHAT_TYPE_LABELS.get(row.chat_type, f"type_{row.chat_type}"),
                "chat_id": row.chat_id,
                "message_id": row.message_id,
                "thread_id": row.thread_id,
                "is_thread": bool(row.is_thread),
                "ts_created_at": row.ts_created_at.isoformat(),
            }
        )

    summary = (
        f"{len(mentions)} mention(s) since {since_dt.date().isoformat()}"
        if mentions
        else f"No mentions since {since_dt.date().isoformat()}."
    )

    return {
        "since": since_dt.isoformat(),
        "mentions": mentions,
        "__summary__": summary,
    }


LIST_MY_MENTIONS = Tool(
    name="list_my_mentions",
    description=(
        "Recent `@mentions` of the current user across chat rooms. "
        "Sourced from `MentionFact`. Use for 'who @mentioned me?', 'am "
        "I tagged in any chat?', 'recent mentions of me'. Returns chat "
        "coordinates (chat_type, chat_id, thread_id, message_id) so the "
        "agent can pair with `fetch_chat_thread` to read the message "
        "body. Chat type labels: dm (1) / gm (2) / pm (3) / mdm (4). "
        "Default window is the past 30 days; pass `since` (ISO date) to "
        "widen or narrow. Caller's mentions only."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "since": {
                "type": "STRING",
                "description": (
                    "Earliest creation timestamp as ISO 8601, e.g. "
                    "'2026-04-27' or '2026-04-27T00:00:00Z'. Default: "
                    f"past {_DEFAULT_SINCE_DAYS} days."
                ),
            },
            "limit": {
                "type": "INTEGER",
                "description": (
                    f"Max mentions to return (1–{_MAX_LIMIT}). Default " f"{_DEFAULT_LIMIT}."
                ),
            },
        },
        "required": [],
    },
    run=_run,
)
