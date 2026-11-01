"""Reading the error entries `opensearchpy.helpers.bulk` hands back.

Shared by the ingest path (`ingestion.py`), the delete paths
(`purge.py`, `ingestion._delete_stale`, `services/demo_seeder.py`) and
`repair.py`, so they all agree on three questions: what status did this
entry fail with, WHICH document did it fail for, and is the failure the
kind a retry can fix.

Entry shapes (`helpers.bulk(..., raise_on_error=False,
raise_on_exception=False)`):

  * the server answered, one item failed:
    `{<op>: {"_id": ..., "status": 400, "error": {"type": ..., "reason": ...}}}`
  * the request itself failed (connection reset, timeout, a 5xx for the
    whole request): one entry per action in that request,
    `{<op>: {"_id": ..., "status": <int or "N/A">, "error": "<str>", "exception": <exc>}}`.
    `status` is the string `"N/A"` when no HTTP response arrived at all.
    Non-delete entries also carry `"data"`, i.e. the whole document, chunk
    text included, so never log an entry itself (MIP-622).
"""

from __future__ import annotations

from typing import Iterable, Optional

VERSION_CONFLICT = "version_conflict_engine_exception"
DOCUMENT_MISSING = "document_missing_exception"


def error_info(err) -> tuple[Optional[object], Optional[str]]:
    """(status, error type) of one entry.

    Written defensively (and without naming the op): this is an error path,
    and a crash here would replace a logged warning with a traceback that
    aborts the run. `status` is returned as found, so a transport failure's
    `"N/A"` survives to `is_transient`.
    """
    action = _action(err)
    if action is None:
        return None, None
    etype = action.get("error")
    etype = etype.get("type") if isinstance(etype, dict) else None
    return action.get("status"), etype


def error_id(err) -> Optional[str]:
    """The `_id` the entry failed for, or None when it doesn't say."""
    action = _action(err)
    if action is None:
        return None
    cid = action.get("_id")
    return str(cid) if cid is not None else None


def error_op(err) -> Optional[str]:
    """The op type (`index` / `update` / `delete` / ...) the entry echoes."""
    if not isinstance(err, dict):
        return None
    return next(iter(err.keys()), None)


def _action(err) -> Optional[dict]:
    if not isinstance(err, dict):
        return None
    action = next(iter(err.values()), None)
    return action if isinstance(action, dict) else None


def is_transient(status, etype: Optional[str] = None) -> bool:
    """Whether a retry can succeed: 429 / 408, any 5xx, no HTTP response at
    all (`"N/A"`, or a malformed entry), or a `cluster_block_exception`
    (403; a flood-stage disk watermark clears itself once space is freed).

    Anything else, a 400 `mapper_parsing_exception` above all, is the
    document's own fault: re-sending the same bytes fails the same way.
    """
    if not isinstance(status, int):
        return True
    if status in (408, 429) or status >= 500:
        return True
    return status == 403 and etype == "cluster_block_exception"


def failed_ids(errors: Iterable, attempted: Iterable[str]) -> set[str]:
    """Ids the given error entries failed for.

    An entry with no `_id` can't be attributed, so the whole attempt counts
    as failed: re-sending what already landed is harmless, while believing
    something landed that didn't is the bug this module exists to prevent.
    """
    out: set[str] = set()
    for err in errors:
        cid = error_id(err)
        if cid is None:
            return set(attempted)
        out.add(cid)
    return out
