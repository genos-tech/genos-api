"""Where a task hangs in the hierarchy: its parent and its project.

The helpers below were private to `origin/views/task/task_views.py`. They
moved here, unchanged, so the agent's `update_task` tool (MIP-899) can
re-parent a task or move it to another project with exactly the links the
UI's PUT writes, without importing a DRF view. The view keeps thin aliases,
so its PUT behavior is unchanged.
"""

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster


def is_nested_task(task) -> bool:
    """Is this task filed under another TASK, so that it follows that
    task's project?

    A milestone, a ROOT task (no parent) and a task living DIRECTLY in a
    milestone may change project. Everything below an ordinary task follows
    it, and travels when it moves.

    A task directly in a milestone hangs off the milestone's backing row via
    `parent_task_id` (see `_bridge_milestone_to_parent`). It was once exempt
    from this rule, was then locked (commit 4af00625: "the milestone owns
    which project its tasks are in"), and is exempt again (MIP-786): people move a
    task out of "Backlog" into another project's milestone, and the move is a
    detach, not a copy. What made the first version lose rows, a parent left
    behind in the project the task came from, is handled by the PUT's project
    change: it clears the stale milestone, sprint and the parent edge when it
    pointed at that milestone's backing row (the bridge's CLEAR branch), and
    carries the task's own sub-tree along. The destination milestone is a
    second step, or the same request when the client names one.

    A dangling `parent_task_id` (parent hard-deleted) reads as root, the same
    fallback `_resolve_root_task_id` makes, so a move is never blocked on data
    we can no longer resolve, which would strand the row in its project for
    good.
    """
    if task.parent_task_id is None or task.is_milestone:
        return False
    parent_is_milestone = (
        TaskMaster.objects.filter(task_id=task.parent_task_id)
        .values_list("is_milestone", flat=True)
        .first()
    )
    if parent_is_milestone is None:  # dangling parent
        return False
    return not parent_is_milestone


def normalize_task_id(value):
    """Coerce a client-supplied task id to an int, mapping every flavor
    of "no parent" (absent, None, empty, the string "null") to None.

    The frontend sends `parent_task_id` as both a number and a string
    depending on the caller, and the comparisons below decide whether a
    reparent happened — so `"12" != 12` would silently look like a move.
    """
    if value in (None, "", "null"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def resolve_root_task_id(task_id, parent_task_id, depth_limit=64):
    """The top of the parent chain a task sits in, given the parent edge
    it is ABOUT to have.

    Mirrors `set_root_task_id` (which only ever runs on create) for the
    update path. Takes the parent explicitly because the caller resolves
    this before writing the new edge to the row.
    """
    if parent_task_id is None:
        return task_id
    current = parent_task_id
    visited = {task_id}
    for _ in range(depth_limit):
        if current in visited:
            # Corrupt data: a cycle. Anchor here rather than spin.
            return current
        visited.add(current)
        row = (
            TaskMaster.objects.filter(task_id=current)
            .values_list("task_id", "parent_task_id")
            .first()
        )
        if row is None:
            # The parent has been hard-deleted. Anchor on the task
            # itself rather than on a row nothing can load — same
            # fallback `set_root_task_id` makes.
            return task_id
        if row[1] is None:
            return current
        current = row[1]
    return current


def milestone_for_parent(parent_task_id):
    """The milestone a task inherits by hanging under `parent_task_id`.

    Either the parent IS a milestone's backing task (so the child lives
    directly in that milestone), or the parent is an ordinary task and
    the child inherits whatever milestone the parent itself lives in.
    Mirrors the inference `TaskMasterView.post` runs at create time.
    """
    if parent_task_id is None:
        return None
    backed = (
        MilestoneMaster.objects.filter(task_id=parent_task_id, is_deleted=False)
        .values_list("milestone_id", flat=True)
        .first()
    )
    if backed is not None:
        return backed
    return (
        TaskMaster.objects.filter(task_id=parent_task_id)
        .values_list("milestone_id", flat=True)
        .first()
    )


def reparent_task(task, parent_task_id):
    """Settle a task's own links after its parent edge changed with no
    `milestone` key in the payload.

    The diagram's structure edges patch `parent_task_id` alone, so
    without this a task dragged under a row in another milestone kept
    the milestone — and the root — of the place it came from. Which
    milestone a task belongs to and which chain it roots at are both
    consequences of where it hangs, so a parent change has to recompute
    both.

    Milestone backing tasks are left alone: their `milestone_id` points
    at the milestone they back, and deriving it from a parent would
    break that link.
    """
    if task.is_milestone:
        return
    milestone_id = milestone_for_parent(parent_task_id)
    sprint_id = None
    if milestone_id is not None:
        sprint_id = (
            MilestoneMaster.objects.filter(milestone_id=milestone_id)
            .values_list("sprint_id", flat=True)
            .first()
        )
    task.parent_task_id = parent_task_id
    task.milestone_id = milestone_id
    # Sprint is always derived from the milestone (there's no direct
    # sprint picker on a task), so it follows the milestone here too.
    task.sprint_id = sprint_id
    task.root_task_id = resolve_root_task_id(task.task_id, parent_task_id)
    task.save(
        update_fields=[
            "milestone_id",
            "parent_task_id",
            "root_task_id",
            "sprint_id",
            "ts_updated_at",
        ]
    )
