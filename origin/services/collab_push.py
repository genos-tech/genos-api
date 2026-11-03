"""Push a server-written task or note body into its collaborative Yjs doc
(MIP-694, MIP-697).

A task body lives in two stores: `TaskMaster.content` (what the agent / MCP
`update_task` tool writes) and the Yjs doc `task-body:<id>` that genos-collab
persists and the editor actually renders. A note is the same: its `body`
(written by `update_note`) and the doc `my-note:<id>` / `task-note:<id>` /
`chat-note:<id>`.
The editor seeds from the row only while that doc is empty, so once a task
or note had been opened an API body write never reached it: the stale doc
kept showing, and the next keystroke autosaved the old body back over the
new one.

`push_task_body` / `push_note_body` close the gap from the writer's side.
After the row commits, they post the new blocks to genos-collab, which
applies them into
the live doc as one diffed Yjs transaction (open editors receive it as a
broadcast, stale IndexedDB replicas converge on sync, comment threads in
the same doc are untouched). See genos-collab `bodyReplace.js`.

Best-effort by design: the body is already saved, so a collab outage must
not fail the tool call. A failed push is logged and leaves the old
behaviour (stale editor) for that one write.

Only `update_task` and `update_note` call this. The REST PATCH/PUT is the
editor's own autosave, already in sync with the doc it came from, and a newly
created task or note has no doc yet (the editor seeds it on first open). The
in-app agent's approved `update_note` also re-applies the body from the
browser (genos-frontend `applyNoteBodyToYjs`); applying the same blocks twice
is a no-op diff, and that client path predates this one and covers nothing
an MCP caller has — there is no browser behind an MCP write.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import jwt
import requests
from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

REPLACE_BLOCKS_PATH = "/internal/documents/replace-blocks"
# Must match genos-collab `INTERNAL_AUDIENCE`. User access tokens carry no
# `aud`, so collab refuses them on this endpoint; and this token has no
# `token_type`, so simplejwt refuses it as a client token.
INTERNAL_AUDIENCE = "genos-collab-internal"
TOKEN_TTL = timedelta(seconds=60)
TIMEOUT_SECONDS = 5


def _internal_token() -> str:
    # Its own secret, not the user-token key (MIP-621): collab holds a copy
    # of whatever signs this, and with the JWT key that copy could mint user
    # tokens for this API.
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"aud": INTERNAL_AUDIENCE, "iat": now, "exp": now + TOKEN_TTL},
        settings.COLLAB_INTERNAL_SECRET,
        algorithm="HS256",
    )


# The editors' document names (genos-frontend `bnMyNoteEditor` /
# `bnTaskNoteEditor` / `bnChatNoteEditor`). genos-collab only accepts these
# and `task-body:`.
_NOTE_DOCUMENT_PREFIX = {"personal": "my-note", "task": "task-note", "chat": "chat-note"}


def _post(document_name: str, blocks: list[dict[str, Any]]) -> bool:
    """One push to genos-collab. True only when collab applied it (HTTP 200)."""
    url = f"{settings.COLLAB_INTERNAL_URL}{REPLACE_BLOCKS_PATH}"
    try:
        resp = requests.post(
            url,
            json={"documentName": document_name, "blocks": blocks},
            headers={"Authorization": f"Bearer {_internal_token()}"},
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        logger.warning("collab body push failed doc=%s: %s", document_name, exc)
        return False
    if resp.status_code != 200:
        logger.warning("collab body push refused doc=%s status=%s", document_name, resp.status_code)
        return False
    return True


def _push(document_name: str, blocks: list[dict[str, Any]]) -> None:
    """Send `blocks` to `document_name` once the current transaction
    commits (immediately when there is none). A rolled-back write never
    pushes. No-op when `COLLAB_INTERNAL_URL` is unset, or when
    `COLLAB_INTERNAL_SECRET` is (production refuses to boot that way, see
    `_validate_transport`; dev just skips the push)."""
    if not settings.COLLAB_INTERNAL_URL:
        return
    if not settings.COLLAB_INTERNAL_SECRET:
        logger.warning(
            "collab body push skipped doc=%s: COLLAB_INTERNAL_SECRET is unset", document_name
        )
        return
    snapshot = list(blocks or [])
    transaction.on_commit(lambda: _post(document_name, snapshot))


class PushOutcome(str, Enum):
    """What happened to a push, for a caller that has to tell the user."""

    SENT = "sent"  # collab applied it: an open editor has the new text
    FAILED = "failed"  # collab refused it or could not be reached
    SKIPPED = "skipped"  # collab is not configured here, so there is no live editor to reach
    DEFERRED = "deferred"  # inside a transaction: sent after commit, outcome not known yet


def _in_transaction() -> bool:
    return transaction.get_connection().in_atomic_block


def _push_now(document_name: str, blocks: list[dict[str, Any]]) -> PushOutcome:
    """Like `_push`, but tells the caller whether the live document got it.

    Outside a transaction the push happens now and its result is returned. Inside
    one it is deferred to commit exactly as `_push` does (a rolled-back write must
    never push), and the caller is told it is `DEFERRED` rather than given a
    result it can't know.
    """
    if not settings.COLLAB_INTERNAL_URL:
        return PushOutcome.SKIPPED
    if not settings.COLLAB_INTERNAL_SECRET:
        logger.warning(
            "collab body push skipped doc=%s: COLLAB_INTERNAL_SECRET is unset", document_name
        )
        return PushOutcome.SKIPPED
    snapshot = list(blocks or [])
    if _in_transaction():
        transaction.on_commit(lambda: _post(document_name, snapshot))
        return PushOutcome.DEFERRED
    return PushOutcome.SENT if _post(document_name, snapshot) else PushOutcome.FAILED


def push_task_body(task_id: int, blocks: list[dict[str, Any]]) -> None:
    """Push a task body into `task-body:<task_id>`."""
    _push(f"task-body:{task_id}", blocks)


def push_note_body(note_type: str, note_id: int, blocks: list[dict[str, Any]]) -> None:
    """Push a note body into `my-note:<id>` (note_type "personal"),
    `task-note:<id>` ("task") or `chat-note:<id>` ("chat")."""
    _push(f"{_NOTE_DOCUMENT_PREFIX[note_type]}:{note_id}", blocks)


def push_note_body_now(note_type: str, note_id: int, blocks: list[dict[str, Any]]) -> PushOutcome:
    """`push_note_body`, but returns whether the live editor got the body.

    A restore must not report success it can't stand behind: if the push fails, an
    editor that is open keeps the old text and its next autosave writes it back
    over the restored one. The caller says so instead (MIP-883).
    """
    return _push_now(f"{_NOTE_DOCUMENT_PREFIX[note_type]}:{note_id}", blocks)
