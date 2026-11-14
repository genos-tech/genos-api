"""A task's files, as METADATA, for `fetch_task` / `get_task` and `delete_task_attachment` (MIP-1563).

The Attachments tab lists, downloads and deletes a task's files; the agent and MCP could do none of it.
Listing and deleting reach them now, behind `AGENT_PARITY_TOOLS` (uploading stays in the app: a
file has nowhere to travel through a `dict -> dict` tool, `MCP_SERVER.md` §7).

What the model is shown of a file, and what it is NEVER shown:

  * shown: `attachment_id` (the task's own number for it, what the delete takes), `name`, `type`,
    `size_bytes`, `uploaded_by` and `uploaded_at`;
  * never: the bytes, and never a link to them. A signed or public URL in the model's context is a
    way for it to leak the file, and the model cannot open one anyway.

The name and the type are text a person typed (the file's own name; the `type` the uploading client
sent), so both are fenced like every other piece of workspace text and cut to a sane length.

`uploaded_by` has no column of its own: the row records no uploader. The task's history does (the
`attachment_added` row names its actor), so it is read from there, and it is `null` when there is
none (a file older than the history, or added with no one signed in). `attachment_id` is the task's
highest id plus one, so an id can be reused after the newest file is deleted: the history row of the
EARLIER file with that id must not be taken for this one, so a row older than the attachment itself
does not count.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskAttachments, TaskMaster
from origin.search_engine.agent.tools.base import ToolContext, wrap_workspace_content

#: Appended to `fetch_task`'s description while `AGENT_PARITY_TOOLS` is on (`tool_flag_text`). MCP's
#: `get_task` shows it too, with the tool names MCP knows.
DESCRIPTION_ADDITION = (
    " `attachments` lists the task's files (`attachment_id`, `name`, `type`, `size_bytes`, "
    "`uploaded_by`, `uploaded_at`), never their content or a link."
)

#: Files listed on one task. The rest are counted, not listed: a task with hundreds of files must not
#: cost hundreds of storage lookups on every read of it.
LISTED_CAP = 50
#: What of a name or a type reaches the model.
NAME_CHARS = 200
TYPE_CHARS = 100
#: The history row is written a few milliseconds after the file's own row. A row older than the file
#: by more than this belongs to an earlier file that had the same id.
_HISTORY_SLACK = timedelta(seconds=5)


def attachment_name(attachment: TaskAttachments) -> str:
    """The file's name as the Attachments tab shows it: the name it was uploaded with, else the
    stored file's own name, never the storage path (which carries the task's id)."""
    return attachment.original_filename or os.path.basename(attachment.attached_file.name or "")


def attachment_size(attachment: TaskAttachments) -> int | None:
    """The file's size in bytes from storage, without opening it; `None` when the file is gone."""
    if not attachment.attached_file:
        return None
    try:
        return int(attachment.attached_file.size)
    except Exception:  # noqa: BLE001 - a missing or unreadable file has no size to report
        return None


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _uploaders(task: TaskMaster, attachments: list[TaskAttachments]) -> dict[int, str]:
    """`attachment_id -> uploader's user id` for the files whose history row names one (one query)."""
    ids = [a.attachment_id for a in attachments]
    if not ids:
        return {}
    rows = (
        TaskActivity.objects.filter(
            task=task,
            action_type=TaskActivityActionType.ATTACHMENT_ADDED,
            metadata__attachmentId__in=ids,
        )
        .exclude(actor__isnull=True)
        .order_by("-activity_id")
        .values_list("actor_id", "metadata", "ts_created_at")
    )
    # Newest row first, so the first row that fits a file is the one that added IT.
    by_id: dict[int, list[tuple[str, Any]]] = {}
    for actor_id, metadata, written_at in rows:
        file_id = (metadata or {}).get("attachmentId")
        if isinstance(file_id, int) and not isinstance(file_id, bool):
            by_id.setdefault(file_id, []).append((str(actor_id), written_at))
    out: dict[int, str] = {}
    for attachment in attachments:
        for actor_id, written_at in by_id.get(attachment.attachment_id, []):
            if written_at is not None and written_at >= attachment.ts_created_at - _HISTORY_SLACK:
                out[attachment.attachment_id] = actor_id
                break
    return out


def attachment_fields(task: TaskMaster, ctx: ToolContext) -> dict[str, Any]:
    """`{"attachments": [...]}` for a task the caller may read (add `attachments_total` past the
    cap). An empty task answers `[]`, which says "no files" and not "not asked"."""
    # Imported here: `label_acl` imports `tools.base`, which runs this package's `__init__`, which
    # imports `fetch_task`, which imports this module.
    from origin.search_engine.agent.label_acl import LabelAccess  # noqa: PLC0415

    rows = list(TaskAttachments.objects.filter(task=task.task_id).order_by("attachment_id"))
    total = len(rows)
    listed = rows[:LISTED_CAP]
    uploader_ids = _uploaders(task, listed)
    names = LabelAccess(ctx).usernames(set(uploader_ids.values())) if uploader_ids else {}
    files = []
    for attachment in listed:
        uploader = uploader_ids.get(attachment.attachment_id)
        files.append(
            {
                "attachment_id": attachment.attachment_id,
                "name": wrap_workspace_content(_cut(attachment_name(attachment), NAME_CHARS)),
                "type": wrap_workspace_content(_cut(attachment.attached_type or "", TYPE_CHARS))
                or None,
                "size_bytes": attachment_size(attachment),
                "uploaded_by": (
                    {
                        "user_id": uploader,
                        "username": wrap_workspace_content(names[uploader])
                        if uploader in names
                        else None,
                    }
                    if uploader
                    else None
                ),
                "uploaded_at": attachment.ts_created_at.isoformat()
                if attachment.ts_created_at
                else None,
            }
        )
    out: dict[str, Any] = {"attachments": files}
    if total > len(listed):
        out["attachments_total"] = total
    return out
