"""The inbox side of cross-team sharing: what the notice says, and how it
gets to the person who has to answer it.

Two things live here because both were wrong in the first cut, and both are
the same class of mistake — a request nobody sees is a request nobody
approves, and the whole feature stalls at step one.

**The body must be BlockNote blocks.** The inbox renders `item_body`
through the message preview and reads structured fields from
`item_optionals`. A `{"title", "text"}` dict is the DIGEST shape (item type
6) and the digest is the only card that renders it, so a dict here renders
as an empty card with the request's text nowhere on screen. The ownership
claim already documents this the hard way (`services/ownership_claim.py`).

**The notice has to be pushed.** Inbox types 1-4 are filed BY the sockets
service, which sends the new row to the receiver in the same breath. These
two are filed by Django, so nothing pushes them and the addressee sees
nothing until they reload the page. The fix follows the one existing
precedent, the ownership claim: the requester's client emits a relay event
after its HTTP call, the sockets service reads the card back from Django
with the caller's own token, and `notice_for` shapes it.

Reading it back rather than accepting it from the client is the point — a
relay that took a body and a recipient from the browser would be a way to
push arbitrary inbox content at any user.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

ITEM_TYPE_TEAM_CONNECTION = 7
ITEM_TYPE_EXTERNAL_SHARE = 8

# What each shareable object is called in a sentence a person reads.
# `ExternalGrant.ObjectType`'s own labels are lower-case machine-ish
# ("note folder"), and "channel" is not what the product calls a chat.
OBJECT_LABELS = {
    "channel": "chat",
    "project": "project",
    "note_folder": "note folder",
}


def _paragraph(content: list[dict]) -> dict:
    return {
        "type": "paragraph",
        "props": {"textColor": "default", "textAlignment": "left", "backgroundColor": "default"},
        "content": content,
        "children": [],
    }


def _plain(text: str) -> dict:
    return {"text": text, "type": "text", "styles": {}}


def _named(text: str) -> dict:
    """A team or object name, styled the way every other request styles it."""
    return {"text": text, "type": "text", "styles": {"bold": True, "textColor": "pink"}}


def connection_request_body(*, requesting_team_name: str, addressed_team_name: str) -> list:
    """`item_body` for "team A would like to connect with team B".

    The second sentence is load-bearing. An owner who reads "connect" as
    "give them our data" will decline a request that grants nothing, and
    the one thing this notice has to get across is that connecting is not
    access — every chat, project and folder is still shared one at a time,
    by them, afterwards.
    """
    return [
        _paragraph(
            [
                _named(requesting_team_name),
                _plain(" would like to connect with "),
                _named(addressed_team_name),
                _plain("."),
            ]
        ),
        _paragraph(
            [
                _plain(
                    "Connecting on its own gives them access to nothing. It only lets the "
                    "two teams share a specific chat, project, or note folder with each "
                    "other later — and each one of those is approved separately."
                )
            ]
        ),
    ]


def share_offer_body(*, owner_team_name: str, object_type: str, object_name: str = "") -> list:
    """`item_body` for "team A shared their project X with your team"."""
    label = OBJECT_LABELS.get(object_type, "item")
    first: list[dict] = [_named(owner_team_name), _plain(f" shared a {label} ")]
    if object_name:
        first += [_plain("— "), _named(object_name), _plain(" — ")]
    first.append(_plain("with your team."))
    return [
        _paragraph(first),
        _paragraph(
            [
                _plain(
                    "Accepting admits nobody by itself. Once you accept, your owner and "
                    "editors choose which of your own people join, any time afterwards, "
                    "without asking again."
                )
            ]
        ),
    ]


def notice_for(item) -> dict:
    """The live-delivery payload for one inbox row.

    Keys are the camelCase the client's `InboxItemProps` expects, matching
    what the inbox GET returns — the row goes straight into IndexedDB, so a
    shape that disagrees with the fetched one produces a card that changes
    when the page is reloaded.
    """
    return {
        "receiver": str(item.receiver_id) if item.receiver_id else None,
        "data": {
            "itemId": item.item_id,
            "itemBody": item.item_body,
            "itemType": item.item_type,
            "isRead": item.is_read,
            "requestStatus": item.request_status,
            "tsSent": item.ts_created_at.isoformat(),
            "itemOptionals": item.item_optionals,
        },
    }


def pending_notices(*, item_type: int, key: str, values) -> list[dict]:
    """Unanswered notices whose `item_optionals[key]` is one of `values`.

    Looked up by the id of the thing requested rather than by the inbox row
    id, because the requester never holds the row id: the inbox GET is
    filtered to `receiver`, and they are the sender.

    A list because one action can produce several notices — creating an
    external chat with three guest teams asks three different owners.
    """
    from django.db.models import Q

    from origin.models.common.inbox_models import InboxItems

    keys = [str(v) for v in values if v]
    if not keys:
        return []
    # OR of exact key matches rather than one `__in` on the JSON key
    # transform, which is fragile across backends for JSONField.
    matches = Q()
    for value in keys:
        matches |= Q(**{f"item_optionals__{key}": value})
    items = InboxItems.objects.filter(
        matches,
        item_type=item_type,
        request_status="pending",
        is_deleted=False,
    ).order_by("ts_created_at")
    return [notice_for(item) for item in items]


def notify_team_owner(
    *, team_id, sender, item_type, item_body, item_optionals, push_title, request_status="pending"
):
    """File a cross-team request in the addressed team owner's inbox, and push.

    Written server-side, unlike the older join-request flows where the
    client posts to `/api/v2/inbox/joinXRequest/` after the fact. A
    cross-team request that silently failed to reach anyone would look to
    the asker exactly like one nobody had answered yet.

    Best-effort: the connection or share is already committed, and a
    notification problem must not fail it. Returns the row, or None if
    there was nobody to tell — callers use it only for logging.
    """
    from origin.models.common.inbox_models import InboxItems
    from origin.models.common.team_models import TeamMaster
    from origin.services.webpush_dispatch import schedule_push_for_inbox_item

    try:
        owner_id = (
            TeamMaster.objects.filter(team_id=team_id).values_list("owner_id", flat=True).first()
        )
        if not owner_id:
            return None
        item = InboxItems.objects.create(
            team_id=team_id,
            sender=sender,
            receiver_id=owner_id,
            item_body=item_body,
            item_type=item_type,
            item_optionals=item_optionals,
            is_read=False,
            request_status=request_status,
        )
        schedule_push_for_inbox_item(item, title=push_title)
        return item
    except Exception:  # noqa: BLE001 — never fail the request over its notice
        logger.exception("inbox notification failed for team=%s type=%s", team_id, item_type)
        return None


def _answer_notice(*, team_id, sender, blocks, optionals, push_title):
    """An activity row (item type 0) telling the asking side what happened.

    Not a request — nothing to approve — so it lands on the Activities tab
    with no buttons, and its `request_status` is empty rather than the
    "pending" a real request carries.

    Worth sending at all because a refusal is otherwise SILENT: declined
    connections and shares are withheld from the lists on purpose (a "no"
    the other team never has to justify), so without this the asker sees
    their request simply vanish and cannot tell that from still waiting.
    """
    return notify_team_owner(
        team_id=team_id,
        sender=sender,
        item_type=0,
        item_body=blocks,
        item_optionals=optionals,
        push_title=push_title,
        request_status="",
    )


def notify_connection_answer(conn, responder, accepted: bool):
    """Tell the team that ASKED whether they are connected now."""
    from origin.models.common.team_models import TeamMaster

    def _name(team_id) -> str:
        row = TeamMaster.objects.filter(team_id=team_id).values("team_name").first()
        return row["team_name"] if row else ""

    asking_team_id = conn.requested_by_team_id
    lo, hi = str(conn.team_lo_id), str(conn.team_hi_id)
    other = hi if lo == str(asking_team_id) else lo
    other_name = _name(other)
    verb = (
        " accepted your connection request."
        if accepted
        else " declined your connection request."
    )
    return _answer_notice(
        team_id=asking_team_id,
        sender=responder,
        blocks=[
            _paragraph([_named(other_name), _plain(verb)]),
            _paragraph(
                [
                    _plain(
                        "You can now share a chat, project, or note folder with them."
                        if accepted
                        else "Nothing was shared, and you can ask again later."
                    )
                ]
            ),
        ],
        optionals={"kind": "team_connection_answer", "connection_id": str(conn.id)},
        push_title=f"{other_name}{verb}",
    )


def notify_share_answer(grant, responder, accepted: bool):
    """Tell the HOST whether the team they lent something to took it."""
    from origin.models.common.team_models import TeamMaster
    from origin.services.external_grants import object_display_name

    def _name(team_id) -> str:
        row = TeamMaster.objects.filter(team_id=team_id).values("team_name").first()
        return row["team_name"] if row else ""

    guest_name = _name(grant.guest_team_id)
    object_name = object_display_name(grant.object_type, grant.object_id)
    label = OBJECT_LABELS.get(grant.object_type, "item")
    verb = " accepted the " if accepted else " declined the "
    return _answer_notice(
        team_id=grant.owner_team_id,
        sender=responder,
        blocks=[
            _paragraph(
                [
                    _named(guest_name),
                    _plain(verb),
                    _plain(f"{label} you shared"),
                    *([_plain(", "), _named(object_name)] if object_name else []),
                    _plain("."),
                ]
            ),
            _paragraph(
                [
                    _plain(
                        "Their owner and editors decide which of their people join, so who "
                        "is in it can change without asking you again."
                        if accepted
                        else "Nobody from their team has access."
                    )
                ]
            ),
        ],
        optionals={
            "kind": "external_share_answer",
            "grant_id": str(grant.id),
            "object_type": grant.object_type,
            "object_id": grant.object_id,
        },
        push_title=f"{guest_name}{verb}{object_name or label} you shared",
    )


def notify_share_offer(grant, actor):
    """Tell the guest team's owner that something was shared with them.

    Called from every path that creates a grant, which is more than one:
    `/team/share/` offers an existing object, and creating an external chat
    offers the new channel in the same request. The second one was silent
    at first — the guest team was in a chat nobody had told them about, and
    could neither find it nor staff it.
    """
    from origin.models.common.team_models import TeamMaster
    from origin.services.external_grants import object_display_name

    def _name(team_id) -> str:
        row = TeamMaster.objects.filter(team_id=team_id).values("team_name").first()
        return row["team_name"] if row else ""

    owner_team_name = _name(grant.owner_team_id)
    object_name = object_display_name(grant.object_type, grant.object_id)
    return notify_team_owner(
        team_id=grant.guest_team_id,
        sender=actor,
        item_type=ITEM_TYPE_EXTERNAL_SHARE,
        item_body=share_offer_body(
            owner_team_name=owner_team_name,
            object_type=grant.object_type,
            object_name=object_name,
        ),
        item_optionals={
            "grant_id": str(grant.id),
            "object_type": grant.object_type,
            "object_id": grant.object_id,
            "object_name": object_name,
            "owner_team_name": owner_team_name,
        },
        push_title=(
            f"{owner_team_name} shared "
            f"{object_name or OBJECT_LABELS.get(grant.object_type, 'something')} with your team"
        ),
    )
