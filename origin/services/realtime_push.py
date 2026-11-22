"""Tell genos-sockets that a task comment changed, so other people's open tasks
update live (MIP-1348).

The app announces a comment typed in the app: the `task_comment` socket handler
in genos-sockets calls this API and then broadcasts to the project room. A
comment written any other way never reached that handler — the agent's and MCP's
`add_comment` / `update_comment` / `delete_comment`, undo, any plain REST
client — so it was saved and announced to nobody: a colleague's open task showed
it only after being reopened.

After such a write commits, `push_task_comment` posts the change to genos-sockets
(`POST /internal/broadcast/task-comment`), which sends it to `prj-<project id>`
in the event shape the app already handles. Shaped like `collab_push`: signed
with its own secret, after commit (a rolled-back write never pushes), and
best-effort — the comment is already saved, so an outage costs one live update
and never fails the write.

The comment written through the socket handler is announced by that handler as
well, so it can arrive twice. That is harmless: the app answers a "comment"
event by refetching the list, deduplicated while one is in flight, and sets its
count chip from the number the event carries rather than adding to it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import requests
from django.conf import settings
from django.db import transaction

from origin.models.task.task_models import TaskMaster

logger = logging.getLogger(__name__)

BROADCAST_PATH = "/internal/broadcast/task-comment"
ACTIVITY_PATH = "/internal/broadcast/activity"
MESSAGE_PATH = "/internal/broadcast/message"
MESSAGES_DELETED_PATH = "/internal/broadcast/messages-deleted"
INBOX_PATH = "/internal/broadcast/inbox"
MEMBERSHIP_PATH = "/internal/membership/removed"
# Must match genos-sockets `utils/internal_auth`.
TIMESTAMP_HEADER = "X-Genos-Timestamp"
SIGNATURE_HEADER = "X-Genos-Signature"
# A write path waits for this on commit, so keep it short: sockets is on the
# private network and answers in milliseconds, or it is down.
TIMEOUT_SECONDS = 2

CREATED = "created"
EDITED = "edited"
DELETED = "deleted"
# A chat message that was rewritten (`push_message`); the comment pushes call that "edited".
UPDATED = "updated"


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256
    ).hexdigest()
    return "sha256=" + digest


# What a failed push may say about itself (MIP-1724): the ids that exist in THIS kind of payload, so
# the line names the chat, project or task that was not announced. Keys are the payload's own;
# values are ids or fixed names, never a title, a body, a member list or a token.
_LOG_TOP_LEVEL = (
    ("userId", "user"),
    ("teamId", "team"),
    ("projectId", "project"),
    ("channelId", "channel"),
    ("taskId", "task"),
    ("commentId", "comment"),
    ("kind", "kind"),
    ("scope", "scope"),
    ("event", "event"),
    ("action", "action"),
)
# A message or activity push nests its ids inside the object it carries.
_LOG_NESTED = (
    ("message", (("id", "message"), ("channelId", "channel"))),
    ("activity", (("id", "activity"), ("channelId", "channel"), ("messageId", "message"))),
)


def _log_fields(payload: dict[str, Any]) -> str:
    """`user=... channel=... kind=...` for the ids a push payload carries. Absent keys are left
    out (the old line printed `task=None comment=None` for every push that was not a comment)."""
    found: dict[str, str] = {}
    for key, label in _LOG_TOP_LEVEL:
        value = payload.get(key)
        if value is not None:
            found[label] = str(value)[:64]
    for name, keys in _LOG_NESTED:
        inner = payload.get(name)
        if isinstance(inner, dict):
            for key, label in keys:
                value = inner.get(key)
                if value is not None:
                    found.setdefault(label, str(value)[:64])
    # A membership push names the rooms it removes: show the first few, and how many more.
    # A bulk message push names how many messages, never which.
    ids = payload.get("messageIds")
    if isinstance(ids, list):
        found["messages"] = str(len(ids))
    for key, label in (("projectIds", "projects"), ("channels", "channels")):
        items = payload.get(key)
        if isinstance(items, list) and items:
            ids = [str(i.get("id") if isinstance(i, dict) else i)[:64] for i in items[:3]]
            more = f"+{len(items) - 3}" if len(items) > 3 else ""
            found[label] = ",".join(ids) + more
    return " ".join(f"{label}={value}" for label, value in found.items()) or "no-ids"


def _post(payload: dict[str, Any], path: str = BROADCAST_PATH, what: str = "task-comment") -> bool:
    """One signed push to `path`. True only when sockets accepted it (HTTP 200)."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    # Stamped when it is SENT, not when the write happened: the receiver rejects
    # a stale timestamp, and on commit this can run after a slow request.
    timestamp = str(int(time.time()))
    try:
        resp = requests.post(
            f"{settings.SOCKETS_INTERNAL_URL}{path}",
            data=body,
            headers={
                "Content-Type": "application/json",
                TIMESTAMP_HEADER: timestamp,
                SIGNATURE_HEADER: _sign(settings.SOCKETS_INTERNAL_SECRET, timestamp, body),
            },
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        logger.warning(
            "sockets %s push failed path=%s %s: %s", what, path, _log_fields(payload), exc
        )
        return False
    if resp.status_code != 200:
        logger.warning(
            "sockets %s push refused path=%s %s status=%s",
            what,
            path,
            _log_fields(payload),
            resp.status_code,
        )
        return False
    return True


def push_task_comment(
    task_id: int,
    comment_id: int,
    action: str,
    *,
    task_comment_count: int | None = None,
) -> None:
    """Announce that comment `comment_id` of task `task_id` was `action`ed.

    `action` is `CREATED`, `EDITED` or `DELETED`; a delete must carry the task's
    live comment count (the app SETs its chip from it). Sends once the current
    transaction commits, or at once when there is none. A no-op when
    `SOCKETS_INTERNAL_URL` is unset, and for a task with no project (there is
    no room to announce it to). Never raises.
    """
    try:
        if not settings.SOCKETS_INTERNAL_URL:
            return
        if not settings.SOCKETS_INTERNAL_SECRET:
            logger.warning(
                "sockets task-comment push skipped task=%s: SOCKETS_INTERNAL_SECRET is unset",
                task_id,
            )
            return
        row = (
            TaskMaster.objects.filter(task_id=task_id)
            .values_list("project_id", "project__is_private")
            .first()
        )
        if row is None or row[0] is None:
            return
        payload: dict[str, Any] = {
            "projectId": int(row[0]),
            "taskId": int(task_id),
            "commentId": int(comment_id),
            "action": action,
            "isPrivate": bool(row[1]),
        }
        if action == DELETED:
            payload["taskCommentCount"] = int(task_comment_count or 0)
        # robust: a failure inside the push is logged, never raised out of the commit.
        transaction.on_commit(lambda: _post(payload), robust=True)
    except Exception:  # noqa: BLE001 — the comment is saved; an observer must not break it
        logger.warning("sockets task-comment push could not be scheduled", exc_info=True)


def push_activity(activity) -> None:
    """Announce an `Activity` this API made with no socket handler to relay it, so the
    recipient's open app shows the row (and its toast) now instead of at the next refetch
    (MIP-1432: what a member's AI app did over MCP).

    genos-sockets sends `activity.created` to the recipient's `/v3` user room with the
    activity exactly as `ActivitySerializer` writes it. Sends once the current transaction
    commits, or at once when there is none. A no-op when `SOCKETS_INTERNAL_URL` is unset.
    Never raises: the activity is saved, and the Web Push and the next refetch still find it.
    """
    try:
        if not settings.SOCKETS_INTERNAL_URL:
            return
        if not settings.SOCKETS_INTERNAL_SECRET:
            logger.warning("sockets activity push skipped: SOCKETS_INTERNAL_SECRET is unset")
            return
        from rest_framework.renderers import JSONRenderer  # noqa: PLC0415

        from origin.serializers.chat.unified_serializers import (  # noqa: PLC0415
            ActivitySerializer,
        )

        payload: dict[str, Any] = {
            "userId": str(activity.recipient_id),
            "activity": json.loads(JSONRenderer().render(ActivitySerializer(activity).data)),
        }
        transaction.on_commit(lambda: _post(payload, ACTIVITY_PATH, "activity"), robust=True)
    except Exception:  # noqa: BLE001 — the activity is saved; an observer must not break it
        logger.warning("sockets activity push could not be scheduled", exc_info=True)


def push_inbox_changed(user_id, team_id=None) -> None:
    """Say that `user_id`'s Inbox changed, so their open app syncs it now instead of at the
    next refocus, push or reload (MIP-1627).

    Rows are filed here with no browser to relay them (a digest, reminder or report from a cron,
    an approved agent write) and a read flag flipped on one device is news to the member's
    others. genos-sockets sends `inbox.changed` to the member's `/v3` user room; it carries ids
    only, never a row, and the app answers with the delta sync it already has, with its own token.
    Sends once the current transaction commits, or at once when there is none. A no-op when
    `SOCKETS_INTERNAL_URL` is unset. Never raises: the row is saved and the next sync finds it.
    """
    try:
        if not settings.SOCKETS_INTERNAL_URL:
            return
        if not settings.SOCKETS_INTERNAL_SECRET:
            logger.warning("sockets inbox push skipped: SOCKETS_INTERNAL_SECRET is unset")
            return
        payload: dict[str, Any] = {"userId": str(user_id)}
        if team_id:
            payload["teamId"] = str(team_id)
        transaction.on_commit(lambda: _post(payload, INBOX_PATH, "inbox"), robust=True)
    except Exception:  # noqa: BLE001 — the row is saved; an observer must not break it
        logger.warning("sockets inbox push could not be scheduled", exc_info=True)


def push_activities(activities) -> None:
    """Announce the `Activity` rows an API-side write made (a task comment, an @mention in a
    task body or a note) to each recipient's open app, when no socket handler will (MIP-1660).

    The app's own writes go through a sockets handler, which calls this API and emits the
    activities the API hands back. An MCP or in-app-agent write has no such handler, so its
    comment and mention rows were stored and announced to nobody: no toast, and the Activity
    feed filled in only at the next refetch. Call this ONLY from those paths, never from a view
    a sockets handler relays (that would deliver twice).

    Each activity is sent as the recipient may see it (`activity_minimal_card.activities_wire`:
    a recipient who cannot open the task gets the minimal card, MIP-1593), the shape the sockets
    handlers relay. Sent after commit, or at once with no transaction. Best-effort and never
    raises: the rows are saved, and the Web Push and the next refetch still find them. A row
    sockets refuses (its body limit) is logged and simply not live.
    """
    try:
        if not settings.SOCKETS_INTERNAL_URL:
            return
        if not settings.SOCKETS_INTERNAL_SECRET:
            logger.warning("sockets activity push skipped: SOCKETS_INTERNAL_SECRET is unset")
            return
        rows = [a for a in (activities or []) if a is not None]
        if not rows:
            return
        from rest_framework.renderers import JSONRenderer  # noqa: PLC0415

        from origin.services.activity_minimal_card import activities_wire  # noqa: PLC0415

        payloads: list[dict[str, Any]] = []
        for wire in json.loads(JSONRenderer().render(activities_wire(rows))):
            recipient = wire.get("recipientUserId") if isinstance(wire, dict) else None
            if recipient:
                payloads.append({"userId": str(recipient), "activity": wire})

        def send_all() -> None:
            for payload in payloads:
                _post(payload, ACTIVITY_PATH, "activity")

        if payloads:
            transaction.on_commit(send_all, robust=True)
    except Exception:  # noqa: BLE001 — the rows are saved; an observer must not break them
        logger.warning("sockets activities push could not be scheduled", exc_info=True)


def push_message(message, event) -> None:
    """Announce a chat `Message` this API wrote with no socket handler to relay it, so a
    colleague who has that chat open sees it now instead of at their next sync (MIP-1459:
    the task card posted for a task made over MCP).

    genos-sockets sends `message.created` (`event=CREATED`) or `message.updated`
    (`event=UPDATED`) to the message's channel room, the event the chat already listens on,
    with the message exactly as `MessageSerializer` writes it (the same prefetches the REST
    send uses, so the shape is the one the app's own sends carry). Sends once the current
    transaction commits, or at once when there is none. A no-op when `SOCKETS_INTERNAL_URL`
    is unset. Never raises: the message is saved, and the next sync still finds it.
    """
    try:
        if not settings.SOCKETS_INTERNAL_URL:
            return
        if not settings.SOCKETS_INTERNAL_SECRET:
            logger.warning("sockets message push skipped: SOCKETS_INTERNAL_SECRET is unset")
            return
        from rest_framework.renderers import JSONRenderer  # noqa: PLC0415

        from origin.models.chat.unified_models import Message  # noqa: PLC0415
        from origin.serializers.chat.unified_serializers import MessageSerializer  # noqa: PLC0415
        from origin.views.chat.message_views import _prefetched_messages  # noqa: PLC0415

        fresh = _prefetched_messages(Message.objects.filter(pk=message.pk)).first()
        if fresh is None:
            return
        payload: dict[str, Any] = {
            "event": event,
            "message": json.loads(JSONRenderer().render(MessageSerializer(fresh).data)),
            # For the log line only: sockets reads `event` and `message`.
            "taskId": fresh.task_id,
        }
        transaction.on_commit(lambda: _post(payload, MESSAGE_PATH, "message"), robust=True)
    except Exception:  # noqa: BLE001 — the message is saved; an observer must not break it
        logger.warning("sockets message push could not be scheduled", exc_info=True)


def push_messages_deleted(channel, messages) -> None:
    """Tell the chat's open clients that these messages were force-deleted (MIP-1951): ONE post
    per batch to genos-sockets, which sends `messages.deleted` (the chat and the ids, never any
    text) to the channel's room, so the rows are blanked now instead of at the next sync. Sent after
    the commit; a no-op when sockets is not configured. Never raises: the rows are erased, and the
    next sync still finds them."""
    try:
        if not settings.SOCKETS_INTERNAL_URL or not messages:
            return
        if not settings.SOCKETS_INTERNAL_SECRET:
            logger.warning("sockets message push skipped: SOCKETS_INTERNAL_SECRET is unset")
            return
        payload: dict[str, Any] = {
            "channelId": str(channel.id),
            "channelKind": int(channel.kind),
            "messageIds": [str(m.pk) for m in messages],
        }
        transaction.on_commit(
            lambda: _post(payload, MESSAGES_DELETED_PATH, "messages-deleted"), robust=True
        )
    except Exception:  # noqa: BLE001 — the rows are erased; an observer must not break it
        logger.warning("sockets messages-deleted push could not be scheduled", exc_info=True)


# Sockets takes at most this many of each (genos-sockets `MAX_ROOMS`); a person in more than that
# is cut off from the first ones and the rest at their next reconnect, which is the same answer
# they got before this push existed.
MAX_ROOMS = 500


def push_membership_removed(
    *,
    user_id,
    team_id,
    scope: str,
    project_ids=(),
    channels=(),
) -> None:
    """Tell genos-sockets that `user_id` lost a team, project or chat (MIP-1371), so it takes
    their open sockets out of those rooms now.

    Removing a membership row does not end a socket that already joined a room: the person kept
    receiving that project's task events and that chat's messages until their browser
    reconnected. `scope` is "team", "project" or "channel"; `project_ids` and `channels`
    (`[(kind, id), ...]`) are what they lost, read BEFORE the rows went. Sends once the current
    transaction commits (a rolled-back removal never pushes), best-effort like the comment push:
    the removal is done whatever sockets says, and a sockets outage costs the live cut-off only.
    A no-op when `SOCKETS_INTERNAL_URL` is unset. Never raises.
    """
    try:
        if not settings.SOCKETS_INTERNAL_URL:
            return
        if not settings.SOCKETS_INTERNAL_SECRET:
            logger.warning(
                "sockets membership push skipped user=%s: SOCKETS_INTERNAL_SECRET is unset",
                user_id,
            )
            return
        payload: dict[str, Any] = {
            "userId": str(user_id),
            "teamId": str(team_id),
            "scope": scope,
            "projectIds": [int(p) for p in project_ids][:MAX_ROOMS],
            "channels": [{"kind": int(k), "id": str(i)} for k, i in channels][:MAX_ROOMS],
        }
        transaction.on_commit(lambda: _post(payload, MEMBERSHIP_PATH, "membership"), robust=True)
    except Exception:  # noqa: BLE001 — the removal is done; an observer must not break it
        logger.warning("sockets membership push could not be scheduled", exc_info=True)


# A chat this big is not told live past this many people; the rest see it gone at their next
# refresh (the chat is gone for them either way: the server no longer lists or serves it).
MAX_CHANNEL_DELETE_PUSHES = 500


def push_channel_deleted(*, user_ids, team_id, channel_id, kind) -> None:
    """Tell genos-sockets that a chat is gone, for each of its members (MIP-1705).

    The same notice as a removal from the chat (`push_membership_removed`, scope "channel"): it
    takes their open sockets out of the chat's room, and the app answers by re-reading its chat
    list, which no longer has the chat. One signed post per person, because the sockets route
    takes one `userId`, so the posts go one after the other and STOP at the first failure: a
    sockets outage then costs one timeout, not one per member. Sends once the transaction commits;
    best-effort and never raises: the delete is done whatever sockets says.
    """
    try:
        if not settings.SOCKETS_INTERNAL_URL:
            return
        if not settings.SOCKETS_INTERNAL_SECRET:
            logger.warning("sockets channel-delete push skipped: SOCKETS_INTERNAL_SECRET is unset")
            return
        people = [str(u) for u in user_ids if u][:MAX_CHANNEL_DELETE_PUSHES]
        if not people:
            return

        def send() -> None:
            for person in people:
                payload: dict[str, Any] = {
                    "userId": person,
                    "teamId": str(team_id),
                    "scope": "channel",
                    "projectIds": [],
                    "channels": [{"kind": int(kind), "id": str(channel_id)}],
                }
                if not _post(payload, MEMBERSHIP_PATH, "membership"):
                    logger.warning(
                        "stopped telling members that chat %s was deleted after one failed push",
                        channel_id,
                    )
                    return

        transaction.on_commit(send, robust=True)
    except Exception:  # noqa: BLE001 — the delete is done; an observer must not break it
        logger.warning("sockets channel-delete push could not be scheduled", exc_info=True)


CHANNEL_APPEARED_PATH = "/internal/broadcast/channel"
# genos-sockets sends this header on the create call of its own `channel.create` handler, which
# announces the new chat to its members itself (`channel.created`); see `announced_by_sockets`.
RELAY_HEADER = "X-Genos-Relay"
RELAY_VALUE = "sockets"

# A chat this big is not told live past this many people; the rest see it at their next chat-list
# refresh, which is what they got before this push existed. The same cap as the delete.
MAX_CHANNEL_APPEARED_PUSHES = 500


def announced_by_sockets(request) -> bool:
    """True when `request` is the create call of genos-sockets' own `channel.create` handler,
    which broadcasts `channel.created` to the members itself, so this API must not announce the
    chat a second time (MIP-1719).

    The marker is not a credential and proves nothing: a client that sets it on its own REST
    create only silences the live notice of its own write. Any other caller (a plain REST client)
    sends no such header and its chat IS announced.
    """
    return request.headers.get(RELAY_HEADER) == RELAY_VALUE


def push_channel_appeared(*, user_ids, team_id, channel_id, kind) -> None:
    """Tell genos-sockets that a chat became visible to each of `user_ids` (MIP-1719), so their
    open apps re-read their chat list now instead of at the next refresh, reload or reconnect.

    For a chat that APPEARS with no socket handler to announce it: a group its owner restored
    (MIP-1715), a group made over REST. The app's own `channel.created` is emitted by the sockets
    `channel.create` handler for a chat made in the app, and a delete reaches every member at
    once (`push_channel_deleted`); the undo and a REST create did not. Call this ONLY from those
    paths, never where the app's own handler already announces (that would announce twice).

    The body is facts only: the person, team, chat and kind. No title, no member list. The app
    answers by re-reading its own list with its own token, so what it may see is still decided
    here. One signed post per person (the sockets route takes one `userId`), one after the other,
    STOPPING at the first failure: an outage then costs one timeout, not one per member, and an
    older sockets that has no such route answers 404 once and is logged, so deploy order does not
    matter. At most `MAX_CHANNEL_APPEARED_PUSHES` people. The caller passes the live roster
    (`chat_member_rows`), so a person who is not a member, or whose account is deleted, is never
    told. Sends once the current transaction commits (a rolled-back write announces nothing), or
    at once when there is none. Best-effort and never raises: the chat is saved whatever sockets
    says. A no-op when `SOCKETS_INTERNAL_URL` is unset.
    """
    try:
        if not settings.SOCKETS_INTERNAL_URL:
            return
        if not settings.SOCKETS_INTERNAL_SECRET:
            logger.warning("sockets channel push skipped: SOCKETS_INTERNAL_SECRET is unset")
            return
        people = [str(u) for u in user_ids if u][:MAX_CHANNEL_APPEARED_PUSHES]
        if not people:
            return

        def send() -> None:
            for person in people:
                payload: dict[str, Any] = {
                    "userId": person,
                    "teamId": str(team_id),
                    "channelId": str(channel_id),
                    "kind": int(kind),
                }
                if not _post(payload, CHANNEL_APPEARED_PATH, "channel"):
                    logger.warning(
                        "stopped telling members that chat %s appeared after one failed push",
                        channel_id,
                    )
                    return

        transaction.on_commit(send, robust=True)
    except Exception:  # noqa: BLE001 — the chat is saved; an observer must not break it
        logger.warning("sockets channel push could not be scheduled", exc_info=True)


TODO_PATH = "/internal/broadcast/todo"


def push_todo_changed(user_id, team_id=None) -> None:
    """Tell genos-sockets that `user_id`'s to-dos changed (MIP-1626), so their open To-do page
    re-reads its list now instead of at the next reload.

    A to-do made over MCP, by the in-app agent or by any REST client other than the open page was
    saved and announced to nobody. genos-sockets sends `todo.changed` to that person's own `/v3`
    user room, so every tab of the OWNER hears it and nobody else does. The body is facts only
    (`userId`, `teamId`): a to-do is private to one person, and no title or note ever leaves the
    API; the app re-reads its list with its own token. Sends once the current transaction commits,
    or at once when there is none. A no-op when `SOCKETS_INTERNAL_URL` is unset. Never raises: the
    to-do is saved, and the next load still finds it.

    Callers do not call this per row: `origin.services.todo_push` collects what changed in a
    transaction and sends one push per owner.
    """
    try:
        if not settings.SOCKETS_INTERNAL_URL:
            return
        if not settings.SOCKETS_INTERNAL_SECRET:
            logger.warning("sockets todo push skipped: SOCKETS_INTERNAL_SECRET is unset")
            return
        payload: dict[str, Any] = {"userId": str(user_id)}
        if team_id is not None:
            payload["teamId"] = str(team_id)
        transaction.on_commit(lambda: _post(payload, TODO_PATH, "todo"), robust=True)
    except Exception:  # noqa: BLE001 — the to-do is saved; an observer must not break it
        logger.warning("sockets todo push could not be scheduled", exc_info=True)
