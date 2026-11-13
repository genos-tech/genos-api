"""`list_my_inbox` tool — recent items from the caller's inbox.

Surfaces the `InboxItems` model that backs the in-app `/workspace/inbox`
route. The app splits it in two (`ACTIVITY_INBOX_ITEM_TYPES`), and so does this:

  * Activity — things that happened and need no answer: notices (0), the Genos
    digest (6) and message reminders (9).
  * Requests — everything else, which somebody is waiting on: join team (1),
    project (2) and GM (3), note access (4), a team ownership claim (5), a team
    connection (7) and an external share offer (8). Each carries
    `request_status` ("pending" / "approved" / "rejected").

Use `category` to scope. Default is `all` so a single call answers
"what's in my inbox?".

ACL contract:
  * Tenant guard: ctx.team_id.
  * Scope: `receiver_id == ctx.user_id` (server-trusted). No way for
    the LLM to surface another user's inbox.
  * Soft-delete tombstones (`is_deleted=True`) excluded.
"""

from __future__ import annotations

import uuid
from typing import Any

from origin.models.common.inbox_models import (
    ACTIVITY_INBOX_ITEM_TYPES,
    ITEM_TYPE_GENOS_REPORT,
    InboxItems,
)
from origin.search_engine.agent.tools import inbox_report
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError

_MAX_LIMIT = 50
_DEFAULT_LIMIT = 20
_VALID_CATEGORIES = {"activity", "requests", "all"}

# Mirror of the mapping comment inside InboxItems.item_type (origin/models/
# common/inbox_models.py). Kept here so the tool response is self-descriptive:
# agents don't have to know the numeric code.
_ITEM_TYPE_LABELS = {
    0: "activity",
    1: "join_team_request",
    2: "join_project_request",
    3: "join_gm_request",
    4: "note_access_request",
    5: "team_ownership_claim",
    6: "genos_digest",
    7: "team_connection_request",
    8: "external_share_offer",
    9: "message_reminder",
}


def _is_request(item_type: int) -> bool:
    """The app's rule (and `unread_badge`'s): anything that is not an activity."""
    return item_type not in ACTIVITY_INBOX_ITEM_TYPES


def _model_body(row) -> Any:
    """The body as the model has always seen it. A schedule report (type 10) stored since the
    typed fields (MIP-1207, MIP-1208) carries a `version`, section `key` and `emoji`, `stats`
    and per-item `kind`, `project`, `status` and the rest, for the card to draw. The model saw
    none of them before, so none is passed on (MIP-1235): `{title, text, sections: [{heading,
    items: [{text, link?}]}]}`, whatever else the body holds. A body without a `version`
    (stored before) and every other kind of item are returned as they are. The row itself is
    never changed: the card and the REST payloads keep the typed fields."""
    body = row.item_body
    if (
        row.item_type != ITEM_TYPE_GENOS_REPORT
        or not isinstance(body, dict)
        or "version" not in body
    ):
        return body
    sections = []
    for section in body.get("sections") if isinstance(body.get("sections"), list) else []:
        if not isinstance(section, dict):
            continue
        items = []
        for entry in section.get("items") if isinstance(section.get("items"), list) else []:
            if not isinstance(entry, dict):
                continue
            item = {"text": entry.get("text")}
            if entry.get("link"):
                item["link"] = entry["link"]
            items.append(item)
        sections.append({"heading": section.get("heading"), "items": items})
    return {"title": body.get("title"), "text": body.get("text"), "sections": sections}


def _target(row) -> dict[str, Any] | None:
    """The id a join or access request is answered with, from what the requester
    attached. That is theirs to write, so only an id of the right shape is
    passed on; the names beside it are left out."""
    optionals = row.item_optionals if isinstance(row.item_optionals, dict) else {}
    try:
        if row.item_type == 2:
            return {"project_id": int(optionals["project_id"])}
        if row.item_type == 3:
            return {"gm_id": str(uuid.UUID(str(optionals["gm_id"])))}
        if row.item_type == 4:
            return {"note_type": int(optionals["note_type"]), "note_id": int(optionals["note_id"])}
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    category = args.get("category", "all")
    if category not in _VALID_CATEGORIES:
        raise ToolError(
            f"`category` must be one of {sorted(_VALID_CATEGORIES)} (got {category!r})."
        )

    include_read = bool(args.get("include_read", False))

    try:
        limit = int(args.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError):
        raise ToolError(f"`limit` must be an integer (got {args.get('limit')!r}).")
    limit = max(1, min(limit, _MAX_LIMIT))

    qs = InboxItems.objects.select_related("sender").filter(
        team_id=ctx.team_id,
        receiver_id=ctx.user_id,
        is_deleted=False,
    )
    if category == "activity":
        qs = qs.filter(item_type__in=ACTIVITY_INBOX_ITEM_TYPES)
    elif category == "requests":
        qs = qs.exclude(item_type__in=ACTIVITY_INBOX_ITEM_TYPES)
    if not include_read:
        qs = qs.filter(is_read=False)

    qs = qs.order_by("-ts_created_at")[:limit]

    # MIP-1160, flag off = nothing below changes: a report's label and whole body as before.
    report_view = inbox_report.enabled()
    items: list[dict[str, Any]] = []
    for row in qs:
        sender = row.sender
        body, cut = _model_body(row), None
        label = _ITEM_TYPE_LABELS.get(row.item_type, f"type_{row.item_type}")
        if report_view and row.item_type == ITEM_TYPE_GENOS_REPORT:
            label = inbox_report.REPORT_LABEL
            body, cut = inbox_report.cap_report_body(body)
        items.append(
            {
                "item_id": row.item_id,
                "item_type": row.item_type,
                "item_type_label": label,
                "sender_id": str(sender.id) if sender else None,
                "sender_username": sender.username if sender else None,
                "is_read": row.is_read,
                "request_status": (row.request_status if _is_request(row.item_type) else None),
                "item_body": body,
                "ts_created_at": row.ts_created_at.isoformat(),
            }
        )
        if cut:
            items[-1]["item_body_cut"] = cut
        if target := _target(row):
            items[-1]["target"] = target

    if items:
        unread_n = sum(1 for it in items if not it["is_read"])
        pending_requests = sum(
            1
            for it in items
            if _is_request(it["item_type"]) and it.get("request_status") == "pending"
        )
        summary_bits = [f"{len(items)} inbox item(s)"]
        if not include_read:
            summary_bits[0] = f"{len(items)} unread item(s)"
        elif unread_n:
            summary_bits.append(f"{unread_n} unread")
        if pending_requests:
            summary_bits.append(f"{pending_requests} pending request(s)")
        summary = "; ".join(summary_bits)
    else:
        summary = "No inbox items match." if include_read else "No unread inbox items."

    return {
        "items": items,
        "category": category,
        "include_read": include_read,
        "__summary__": summary,
    }


LIST_MY_INBOX = Tool(
    name="list_my_inbox",
    description=(
        "Recent items from the current user's inbox (`InboxItems`). Two "
        "kinds. Activity: notifications (e.g. 'Alice closed task WRD-5'), the "
        "Genos digest and message reminders. Requests, which someone is "
        "waiting on: join team, project or group chat, note access, a team "
        "ownership claim, a team connection and an external share offer, "
        "each with request_status 'pending'/'approved'/'rejected'. A join-"
        "project, join-GM or note-access request carries a `target` with the "
        "id to answer it with. Use for 'what's in my inbox?', 'any "
        "notifications for me?', 'do I have pending requests?'. Newest "
        "first. By default only unread items are returned; pass "
        "`include_read=true` to include items already marked read. Pass "
        "`category` to scope to 'activity' or 'requests'. Caller's inbox "
        "only — there is no way to specify another user."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "category": {
                "type": "STRING",
                "enum": ["activity", "requests", "all"],
                "description": ("Scope to activity, requests, or both. Default 'all'."),
            },
            "include_read": {
                "type": "BOOLEAN",
                "description": (
                    "If true, include items already marked read. Default false (only unread)."
                ),
            },
            "limit": {
                "type": "INTEGER",
                "description": (f"Max items to return (1–{_MAX_LIMIT}). Default {_DEFAULT_LIMIT}."),
            },
        },
        "required": [],
    },
    run=_run,
)
