"""Push a server-written task body into its collaborative Yjs doc (MIP-694).

A task body lives in two stores: `TaskMaster.content` (what the agent / MCP
`update_task` tool writes) and the Yjs doc `task-body:<id>` that genos-collab
persists and the editor actually renders. The editor seeds from `content`
only while that doc is empty, so once a task had been opened an API body
write never reached it: the stale doc kept showing, and the next keystroke
autosaved the old body back over the new one.

`push_task_body` closes the gap from the writer's side. After the row
commits, it posts the new blocks to genos-collab, which applies them into
the live doc as one diffed Yjs transaction (open editors receive it as a
broadcast, stale IndexedDB replicas converge on sync, comment threads in
the same doc are untouched). See genos-collab `bodyReplace.js`.

Best-effort by design: the body is already saved, so a collab outage must
not fail the tool call. A failed push is logged and leaves the old
behaviour (stale editor) for that one write.

Only `update_task` calls this. The REST PATCH is the editor's own autosave,
already in sync with the doc it came from, and a newly created task has no
doc yet (the editor seeds it from `content` on first open).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
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
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"aud": INTERNAL_AUDIENCE, "iat": now, "exp": now + TOKEN_TTL},
        settings.SIMPLE_JWT["SIGNING_KEY"],
        algorithm="HS256",
    )


def _post(task_id: int, blocks: list[dict[str, Any]]) -> None:
    url = f"{settings.COLLAB_INTERNAL_URL}{REPLACE_BLOCKS_PATH}"
    document_name = f"task-body:{task_id}"
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
        return
    if resp.status_code != 200:
        logger.warning(
            "collab body push refused doc=%s status=%s body=%s",
            document_name,
            resp.status_code,
            resp.text[:200],
        )


def push_task_body(task_id: int, blocks: list[dict[str, Any]]) -> None:
    """Send `blocks` to the task's collab doc once the current transaction
    commits (immediately when there is none). A rolled-back write never
    pushes. No-op when `COLLAB_INTERNAL_URL` is unset."""
    if not settings.COLLAB_INTERNAL_URL:
        return
    snapshot = list(blocks or [])
    transaction.on_commit(lambda: _post(task_id, snapshot))
