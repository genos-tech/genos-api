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
READ_THREADS_PATH = "/internal/documents/read-threads"
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


def read_note_threads(
    note_type: str, note_id: int, *, timeout: float = TIMEOUT_SECONDS
) -> list[dict[str, Any]] | None:
    """The comment threads of one note, read from its Yjs doc (MIP-1064).

    A BlockNote comment lives in the collab doc, not in a table this process
    owns, so the only way to see one is to ask genos-collab. Returns the threads
    exactly as collab answers (author ids, ISO times, comment bodies as BlockNote
    blocks, resolved state, anchor text), `[]` for a note nobody has opened, and
    `None` when they could not be read at all: collab is not configured here, is
    unreachable, refused, or slower than `timeout` (MCP waits the default; the in-app
    agent passes a shorter one, MIP-1545). The caller says so; a note with no comments and
    a note whose comments could not be read must not look alike.
    """
    if not settings.COLLAB_INTERNAL_URL or not settings.COLLAB_INTERNAL_SECRET:
        return None
    document_name = f"{_NOTE_DOCUMENT_PREFIX[note_type]}:{note_id}"
    try:
        resp = requests.post(
            f"{settings.COLLAB_INTERNAL_URL}{READ_THREADS_PATH}",
            json={"documentName": document_name},
            headers={"Authorization": f"Bearer {_internal_token()}"},
            timeout=timeout,
            allow_redirects=False,
        )
        if resp.status_code != 200:
            logger.warning(
                "collab thread read refused doc=%s status=%s", document_name, resp.status_code
            )
            return None
        threads = resp.json().get("threads")
    except (requests.RequestException, ValueError, AttributeError) as exc:
        logger.warning("collab thread read failed doc=%s: %s", document_name, exc)
        return None
    return threads if isinstance(threads, list) else None


REPLY_THREAD_PATH = "/internal/documents/reply-thread"
RESOLVE_THREAD_PATH = "/internal/documents/resolve-thread"


class ThreadWriteError(Exception):
    """A comment-thread write that did not happen (or whose outcome is not known).

    `kind` says what the caller should tell the person:
    - `unavailable`: collab is not configured, not reachable, or answered 5xx. Nothing was written.
    - `unsupported`: collab answered something that is not this endpoint's JSON (an older collab
      answers every unknown path "200 OK"): the comment endpoints are not deployed there yet.
    - `not_found`: no such open thread on that note.
    - `forbidden`: collab refused the actor (only the thread's author or the note's owner may resolve).
    - `refused`: collab rejected the request (a malformed field).
    - `unknown`: the request went out and the answer did not come back, so a reply may or may not
      have been added. Never retried here: a second POST would be a second reply.
    """

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


def _write_thread(path: str, payload: dict[str, Any], expect: str) -> dict[str, Any]:
    """One synchronous POST to a collab thread-write endpoint; the JSON answer when collab
    confirms (HTTP 200 with `ok: true` and the `expect` key), else a `ThreadWriteError`.

    Synchronous on purpose (the caller reports what happened) and never retried (a reply is not
    idempotent). The 200 is not trusted by itself: Hocuspocus answers any unknown path with a
    plain-text "OK", which is what an older collab does to this request.
    """
    if not settings.COLLAB_INTERNAL_URL or not settings.COLLAB_INTERNAL_SECRET:
        raise ThreadWriteError("unavailable", "collab is not configured")
    document = payload.get("documentName")
    try:
        resp = requests.post(
            f"{settings.COLLAB_INTERNAL_URL}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {_internal_token()}"},
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.ConnectTimeout as exc:
        logger.warning("collab thread write not sent doc=%s: %s", document, exc)
        raise ThreadWriteError("unavailable", "collab could not be reached") from exc
    except requests.RequestException as exc:
        logger.warning("collab thread write outcome unknown doc=%s: %s", document, exc)
        raise ThreadWriteError("unknown", "no answer from collab") from exc

    try:
        body = resp.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    status = resp.status_code
    if status == 200:
        if isinstance(body, dict) and body.get("ok") is True and expect in body:
            return body
        logger.warning("collab thread write path=%s answered 200 without ok doc=%s", path, document)
        raise ThreadWriteError("unsupported", "collab does not have this endpoint yet")
    logger.warning("collab thread write refused path=%s doc=%s status=%s", path, document, status)
    if status == 404 and error:
        raise ThreadWriteError("not_found", str(error))
    if status == 403 and error:
        raise ThreadWriteError("forbidden", str(error))
    if status == 400 and error:
        raise ThreadWriteError("refused", str(error))
    if status in (404, 405):
        raise ThreadWriteError("unsupported", "collab does not have this endpoint yet")
    raise ThreadWriteError("unavailable", f"collab answered {status}")


def reply_note_thread(
    note_type: str,
    note_id: int,
    *,
    thread_id: str,
    user_id: str,
    text: str,
    via: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add a comment to a note's comment thread (MIP-1100), as `user_id`.

    The answer is collab's: `commentId`, `commentCount`, `threadResolved`. `via` is how the
    reply arrived, ALREADY reduced to what any reader of the note may know (the doc goes to
    every reader, so a key's name or a connection id must never get here); it is stored on the
    comment's `metadata`.
    """
    payload: dict[str, Any] = {
        "documentName": f"{_NOTE_DOCUMENT_PREFIX[note_type]}:{note_id}",
        "threadId": thread_id,
        "userId": user_id,
        "text": text,
    }
    if via:
        payload["metadata"] = {"via": via}
    return _write_thread(REPLY_THREAD_PATH, payload, "commentId")


def resolve_note_thread(
    note_type: str,
    note_id: int,
    *,
    thread_id: str,
    user_id: str,
    may_resolve_any: bool,
) -> dict[str, Any]:
    """Mark a note's comment thread resolved (MIP-1100), as `user_id`.

    collab resolves it when `user_id` wrote the thread's first comment, or `may_resolve_any` is
    true (the caller is the note's owner), and changes nothing if it is already resolved.
    """
    return _write_thread(
        RESOLVE_THREAD_PATH,
        {
            "documentName": f"{_NOTE_DOCUMENT_PREFIX[note_type]}:{note_id}",
            "threadId": thread_id,
            "userId": user_id,
            "mayResolveAny": bool(may_resolve_any),
        },
        "resolved",
    )
