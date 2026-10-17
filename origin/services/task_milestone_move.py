"""Move a task into, between or out of a milestone.

Extracted from `origin/views/task/task_views.py` so the agent's
`update_task` / `update_tasks_bulk` tools (MIP-692) re-file a task with
the exact linkage the UI's milestone picker produces, without importing
a DRF view. The view keeps thin aliases to these functions, so its PUT
behavior is unchanged.
"""

from django.utils import timezone

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services.milestone_service import ensure_backing_task
from origin.services.task_project_move import collect_descendant_task_ids


def bridge_milestone_to_parent(task, requested_milestone_id, parent_task_id):
    """Keep `parent_task_id` / `root_task_id` in sync with a milestone change.

    Mirrors the bridge logic in `TaskMasterView.post` so PUT updates
    behave the same way as creation. The contract (see
    `MilestoneMaster.task` doc) is that tasks "living in a milestone"
    have `parent_task_id == milestone.task_id` so the project task
    table nests them as sub-tasks of the milestone row.

    `requested_milestone_id` is the value pulled from `request.data`:
        - a milestone id  -> SET / CHANGE the link
        - `None`          -> CLEAR the link (caller must distinguish
                             "key absent" from "key present + null"
                             before invoking this helper)

    Writes are scoped via `save(update_fields=[...])` so the broader
    `serializer.save()` upstream can run independently without us
    stepping on its updates.
    """
    if requested_milestone_id is not None:
        try:
            m = MilestoneMaster.objects.select_related("task").get(
                milestone_id=requested_milestone_id, is_deleted=False
            )
        except MilestoneMaster.DoesNotExist:
            return
        # Don't let a task become its own parent (defensive: would only
        # happen if a milestone's backing task hit this endpoint, which
        # shouldn't be possible via the picker but might via direct
        # API calls).
        if m.task_id is None or m.task_id == task.task_id:
            return
        task.milestone_id = requested_milestone_id
        # Set parent task id to the requested parent task id if it exists, otherwise set it to the milestone's backing task id
        task.parent_task_id = parent_task_id if parent_task_id else m.task_id
        # Root task id is always the milestone's backing task id
        task.root_task_id = m.task_id
        # The frontend doesn't expose a direct sprint picker on tasks
        # — sprint is always inherited from the task's milestone. Sync
        # it here so a task moved into / between milestones lands in
        # the right sprint bucket without the client having to send
        # `sprint` explicitly.
        task.sprint_id = m.sprint_id
        task.save(
            update_fields=[
                "milestone_id",
                "parent_task_id",
                "root_task_id",
                "sprint_id",
                "ts_updated_at",
            ]
        )
    else:
        if task.milestone_id is None:
            return
        # Only break the parent link when the current parent IS the old
        # milestone's backing task. A genuine sub-task chain that
        # happened to share a milestone (e.g. Milestone -> Task A ->
        # Sub-task B; user clears milestone on B) must keep its A->B
        # parent edge intact.
        try:
            old_m = MilestoneMaster.objects.get(milestone_id=task.milestone_id)
            cleared_parent = old_m.task_id is not None and task.parent_task_id == old_m.task_id
        except MilestoneMaster.DoesNotExist:
            cleared_parent = False
        task.milestone_id = None
        # Mirror the milestone clear on the sprint so a row that used
        # to inherit a sprint via its milestone doesn't keep the now-
        # orphaned chip lingering on the table. Tasks without a
        # milestone are unscheduled by definition in the current UX.
        task.sprint_id = None
        update_fields = ["milestone_id", "sprint_id", "ts_updated_at"]
        if cleared_parent:
            task.parent_task_id = None
            task.root_task_id = task.task_id
            update_fields += ["parent_task_id", "root_task_id"]
        task.save(update_fields=update_fields)


def cascade_tree_position_to_subtasks(task, depth_limit=10):
    """Push a task's settled position in the hierarchy onto everything
    below it.

    A task's place in the tree is denormalized across three columns, and
    a move has to carry all of them:

      * `milestone_id` / `sprint_id` — `milestone_views.py` aggregations
        and the sprint board filter on these, so a descendant left
        behind silently under-counts in the milestone it actually
        belongs to and over-counts in the one it left.
      * `root_task_id` — every "rooted-at-chain-top" surface (task
        diagram, sub-task drawer, the diagram shortcut) anchors on it.
        A descendant still pointing at the old chain opens a tree it is
        no longer part of: the diagram for such a sub-task showed
        neither its parent nor its milestone, because it BFS'd down from
        a root the sub-task had left.

    `parent_task_id` is deliberately NOT touched: the edges *inside* the
    moved sub-tree don't change when its top moves, and rewriting them
    would flatten the chain.

    `ts_updated_at` is bumped by hand because a queryset `.update()`
    skips `auto_now` (and `post_save`). Without it the clients that sync
    tasks incrementally (`getProjectTasks?since=`) never learn the
    descendants moved, and keep showing them under the old milestone
    until something forces a full reload.
    """
    collected = collect_descendant_task_ids(task.task_id, depth_limit=depth_limit)
    if not collected:
        return
    TaskMaster.objects.filter(task_id__in=collected).update(
        milestone_id=task.milestone_id,
        sprint_id=task.sprint_id,
        root_task_id=task.root_task_id,
        ts_updated_at=timezone.now(),
    )


def milestone_move_error(task, milestone_id, team_id) -> str | None:
    """Why `task` can't be filed under milestone `milestone_id`, or None
    when it can. `milestone_id` None means detach.

    The picker only offers the task's own project's milestones; a tool
    can name any id, so the same boundaries are checked here.
    """
    # A milestone's backing row carries its OWN milestone_id, so neither
    # filing it elsewhere nor "detaching" it is a move — both would sever
    # the milestone from the row that holds its comments and sub-tasks.
    if task.is_milestone:
        return f"Task {task.display_id} is a milestone; it can't be moved into or out of one."
    if milestone_id is None:
        return None
    milestone = MilestoneMaster.objects.filter(milestone_id=milestone_id, is_deleted=False).first()
    if milestone is None:
        return f"Milestone {milestone_id} not found."
    if str(milestone.team_id or "") != str(team_id):
        return "Not authorized: milestone belongs to a different team."
    if milestone.project_id != task.project_id:
        return (
            f"Milestone {milestone_id} belongs to project {milestone.project_id}, "
            f"not the task's project {task.project_id}."
        )
    if milestone.task_id is not None and milestone.task_id in collect_descendant_task_ids(
        task.task_id
    ):
        return f"Milestone {milestone_id} sits under task {task.display_id}; that would be a cycle."
    return None


def move_task_to_milestone(task, milestone_id) -> bool:
    """File `task` under milestone `milestone_id`, or detach it when
    `milestone_id` is None, and carry its sub-tree along. Validate with
    `milestone_move_error` first.

    The same two steps `TaskMasterView.put` runs for a `milestone` key in
    the payload: the bridge settles the task's own links, then the
    cascade follows only if its position actually changed. The task
    lands directly under the milestone's backing task, as when the
    milestone picker is used. Returns whether anything moved.

    A task already in the milestone is left where it is: re-running the
    bridge would lift a sub-task (Milestone -> A -> B) out of its chain
    and onto the milestone itself.
    """
    task.refresh_from_db()
    if task.milestone_id == milestone_id:
        return False
    if milestone_id is not None:
        # Legacy milestones get their backing row on first access; without
        # one the bridge has nothing to hang the task under and no-ops.
        ensure_backing_task(MilestoneMaster.objects.get(milestone_id=milestone_id))
    before = (task.milestone_id, task.sprint_id, task.root_task_id, task.parent_task_id)
    bridge_milestone_to_parent(task, milestone_id, None)
    task.refresh_from_db()
    if (task.milestone_id, task.sprint_id, task.root_task_id) != before[:3]:
        cascade_tree_position_to_subtasks(task)
    return (task.milestone_id, task.sprint_id, task.root_task_id, task.parent_task_id) != before
