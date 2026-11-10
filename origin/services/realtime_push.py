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


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256
    ).hexdigest()
    return "sha256=" + digest


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
            "sockets %s push failed task=%s comment=%s user=%s: %s",
            what,
            payload.get("taskId"),
            payload.get("commentId"),
            payload.get("userId"),
            exc,
        )
        return False
    if resp.status_code != 200:
        logger.warning(
            "sockets %s push refused task=%s comment=%s user=%s status=%s",
            what,
            payload.get("taskId"),
            payload.get("commentId"),
            payload.get("userId"),
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
