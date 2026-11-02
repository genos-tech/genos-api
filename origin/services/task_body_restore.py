"""Put a task's description back from its history (MIP-738, MIP-880).

`update_task` with `content_text` replaces the WHOLE body, and after it the task
held only the new text. The signal keeps the previous body on the
`description_edited` row (`old_value`, `previousBodyKept`), and this is how it
comes back. The REST view (`TaskBodyRestoreView`) and the agent's
`restore_task_body` tool both call it.
"""

from __future__ import annotations

from typing import Any

from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster


class RestoreRefused(Exception):
    """The restore can't be done. `status` is the HTTP status the page answers."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def restore_task_body(*, task: TaskMaster, activity_id: int, actor_id: Any) -> dict[str, Any]:
    """Restore the description a `description_edited` row of THIS task kept.

    The row is looked up by task as well as by id, so an activity id from another
    task, or one the caller cannot see, is "not found" and never a read of
    someone else's body. It must be a description row that kept its body.

    A restore is itself a body rewrite, and `app` autosave does not keep the
    previous body. So that a mistaken restore can be undone too, the body it
    replaces is recorded explicitly on the row it writes. It also pushes the body
    into the task's live Yjs document (MIP-694): the editor renders that, not
    `content`, so without the push a task that was ever opened keeps showing the
    rewrite.
    """
    # Local imports, as the view had them: the agent tools import services, so a
    # module-level import here would close a loop at startup, and a test that
    # patches `collab_push.push_task_body` only reaches a name read at call time.
    from origin.search_engine.agent.tools.body_mentions import (  # noqa: PLC0415
        notify_task_body_mentions,
    )
    from origin.services.collab_push import push_task_body  # noqa: PLC0415

    row = TaskActivity.objects.filter(
        activity_id=activity_id,
        task_id=task.task_id,
        action_type=TaskActivityActionType.DESCRIPTION,
    ).first()
    if row is None:
        raise RestoreRefused("Activity not found.", 404)
    if not (row.metadata or {}).get("previousBodyKept") or not isinstance(row.old_value, list):
        raise RestoreRefused("That edit did not keep the previous description.", 400)

    previous_body = row.old_value
    replaced_body = task.content

    # A restore must be restorable: the signal keeps the previous body only for
    # agent and API-key writes, and the page's own restore arrives as the app.
    task.content = previous_body
    task._keep_previous_body = True
    task.save(update_fields=["content"])
    push_task_body(task.task_id, previous_body)
    notify_task_body_mentions(task, actor_id=str(actor_id))

    return {
        "taskId": task.task_id,
        "restoredFrom": row.activity_id,
        "replacedBodyKept": isinstance(replaced_body, list),
    }
