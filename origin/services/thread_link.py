"""Chat-thread → task linkage invariants.

A chat thread (DM/GM/MDM) may be the origin of at most ONE task — the
frontend hides its "Create task" action once a thread has a task, but
that gate is client state and races (stale menus, two members creating
at once, old sessions). These helpers give the create paths — the task
finalize PUT and the milestone POST (whose backing TaskMaster row also
carries the linkage) — one shared server-side check.

`chat_id` / `thread_id` are the v3 Channel / Message UUIDs stored as
opaque CharFields on TaskMaster (legacy rows hold ints); comparison is
by exact string match, mirroring `GetTaskByThreadIdView`.
"""

from origin.models.task.task_models import TaskMaster


def find_thread_link_conflict(
    team_id,
    chat_id,
    thread_id,
    *,
    exclude_task_id=None,
):
    """Return the task_id of a live task already linked to this thread,
    or None. `exclude_task_id` skips the row being written (an update
    re-sending its own linkage is not a conflict)."""

    if not chat_id or not thread_id:
        return None
    qs = TaskMaster.objects.filter(
        team_id=team_id,
        chat_id=str(chat_id),
        thread_id=str(thread_id),
        is_init_task=False,
        is_deleted=False,
    )
    if exclude_task_id is not None:
        qs = qs.exclude(task_id=exclude_task_id)
    return qs.values_list("task_id", flat=True).first()
