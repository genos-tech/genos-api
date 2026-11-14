"""Where a task hangs in the hierarchy: its parent and its project.

The helpers below were private to `origin/views/task/task_views.py`. They
moved here, unchanged, so the agent's `update_task` tool (MIP-899) can
re-parent a task or move it to another project with exactly the links the
UI's PUT writes, without importing a DRF view. The view keeps thin aliases,
so its PUT behavior is unchanged.
"""

from django.db import transaction

from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster, claim_project_task_number
from origin.services.task_cache import invalidate_for_task, invalidate_project_tasks_cache
from origin.services.task_milestone_move import (
    bridge_milestone_to_parent,
    cascade_tree_position_to_subtasks,
)
from origin.services.task_project_move import (
    collect_descendant_task_ids,
    relocate_task_satellites,
    relocate_tasks,
)
from origin.views.utils.scope_guards import is_project_member


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


def parent_implied_milestone(parent_task_id):
    """`(found, milestone_id)`: the milestone a task has by hanging under
    `parent_task_id`, with `found` False when no such task exists.

    `milestone_for_parent` answers None both for "the parent is in no
    milestone" and for "there is no such parent". A caller that has to decide
    whether a payload CONTRADICTS the tree needs the two told apart: a
    dangling parent proves nothing, while a parent in no milestone proves the
    child is in none either.
    """
    if parent_task_id is None:
        return False, None
    backed = (
        MilestoneMaster.objects.filter(task_id=parent_task_id, is_deleted=False)
        .values_list("milestone_id", flat=True)
        .first()
    )
    if backed is not None:
        return True, backed
    row = TaskMaster.objects.filter(task_id=parent_task_id).values_list("milestone_id").first()
    if row is None:
        return False, None
    return True, row[0]


def payload_contradicts_tree(requested_milestone_id, parent_task_id) -> bool:
    """Does a payload that files a task in `requested_milestone_id` while
    hanging it under `parent_task_id` disagree with where that parent is?

    A task's milestone is a consequence of where it hangs, so the two cannot
    differ: the parent is the milestone's backing row, or an ordinary task
    that lives in that milestone. The app's full save re-sends BOTH values
    from its working copy on every edit, and a copy that is older than a
    move (a cached task opened after a teammate, a tool or a cascade moved
    its tree) carries the OLD milestone next to the CURRENT parent. Honoring
    that pair wrote a milestone the tree does not have and carried the whole
    sub-tree there (MIP-1168). The milestone picker never sends such a pair:
    it moves the parent with the milestone, to the new backing row.
    """
    if parent_task_id is None:
        return False
    found, implied = parent_implied_milestone(parent_task_id)
    return found and implied != requested_milestone_id


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


def find_parent(parent_task_id, team_id):
    """The live task `parent_task_id` names, as `(task, None)`, or
    `(None, why_not)`. A deleted task, or one in another team, is not a
    parent anyone can file work under."""
    parent = (
        TaskMaster.objects.filter(task_id=parent_task_id, is_deleted=False)
        .exclude(status="Deleted")
        .first()
    )
    if parent is None:
        return None, f"Task {parent_task_id} not found."
    if str(parent.team_id or "") != str(team_id):
        # The words of a missing one, so a guessed id does not say which tasks exist in other
        # teams (MIP-1519).
        return None, f"Task {parent_task_id} not found."
    return parent, None


def parent_move_error(task, parent_task_id, team_id) -> str | None:
    """Why `task` can't hang under `parent_task_id`, or None when it can.
    `parent_task_id` None means "make it top-level".

    The UI only offers parents from the task's own project and a PUT checks
    nothing but the cycle, so a tool that can name any id needs the rest
    here. The caller's access to the parent is the tool's to check, as it
    does for the task itself.
    """
    # A milestone's backing row holds the milestone's comments and
    # sub-tasks, and `reparent_task` leaves it alone for the same reason.
    if task.is_milestone:
        return f"Task {task.display_id} is a milestone; it can't be filed under a task."
    if parent_task_id is None:
        return None
    parent, error = find_parent(parent_task_id, team_id)
    if error:
        return error
    if parent.task_id == task.task_id or parent.task_id in collect_descendant_task_ids(
        task.task_id
    ):
        return "A task cannot be moved under itself or one of its own sub-tasks."
    if parent.project_id != task.project_id:
        return (
            f"Task {parent.display_id} is in project {parent.project_id}, not "
            f"{task.display_id}'s project {task.project_id}. Move the task into that "
            "project first, then set its parent."
        )
    return None


def move_task_to_parent(task, parent_task_id) -> bool:
    """Hang `task` under `parent_task_id` (None = top-level) and carry its
    sub-tree along. Validate with `parent_move_error` first.

    The two steps `TaskMasterView.put` runs for a parent change with no
    `milestone` key: settle the task's own links, then cascade only if its
    place in the tree actually changed. Returns whether the parent changed.
    """
    task.refresh_from_db()
    if task.parent_task_id == parent_task_id:
        return False
    before = (task.milestone_id, task.sprint_id, task.root_task_id)
    reparent_task(task, parent_task_id)
    task.refresh_from_db()
    if (task.milestone_id, task.sprint_id, task.root_task_id) != before:
        cascade_tree_position_to_subtasks(task)
    return True


def project_move_error(task, project_id, team_id, user_id, *, detaching=False) -> str | None:
    """Why `task` can't move to project `project_id`, or None when it can.

    `detaching` says the caller is taking the task out from under its parent
    first, which is what lets a nested task move at all.
    """
    if task.is_milestone:
        return (
            f"Task {task.display_id} is a milestone; move it from the milestone's own "
            "menu in the app."
        )
    destination = ProjectMaster.objects.filter(project_id=project_id, is_deleted=False).first()
    # 404-shaped for a project the caller can't see: ids are sequential.
    if destination is None or not is_project_member(destination.project_id, user_id):
        return f"Project {project_id} not found."
    if str(destination.team_id or "") != str(team_id):
        return f"Project {project_id} not found."
    if not detaching and not task.is_init_task and is_nested_task(task):
        return (
            f"Task {task.display_id} follows the project of the task or milestone it is "
            "filed under. Move that one instead, or take this one out first with "
            "`parent_task_id: ''`."
        )
    return None


def move_task_to_project(task, project_id) -> None:
    """Move `task`, its sub-tree and the rows keyed to it into `project_id`.
    Validate with `project_move_error` first.

    What `TaskMasterView.put` does for a changed `project`: the row's team
    follows its project, its number is re-claimed in the destination, the
    milestone, sprint and any parent edge into the project it left are cut,
    the sub-tree and the satellites (notes, dependency edges) cross with it,
    and both projects' task lists are dropped from the cache. The task's own
    tags are cleared, as the project picker does: they are the old project's.
    """
    destination = ProjectMaster.objects.get(project_id=project_id)
    old_project_id, old_team_id = task.project_id, task.team_id
    with transaction.atomic():
        task.project_id = destination.project_id
        task.team_id = destination.team_id
        # Unique per project: null it so the move can't collide, then
        # re-claim a free number in the destination.
        task.project_task_number = None
        task.tags = None
        task.save(update_fields=["project", "team", "project_task_number", "tags", "ts_updated_at"])
        claim_project_task_number(task)
        task.refresh_from_db()

        # Milestone and sprint belong to the project the task left, so the
        # destination starts without them; the bridge's CLEAR branch also
        # detaches a task whose only parent was that milestone's backing row.
        before = (task.milestone_id, task.sprint_id, task.root_task_id)
        bridge_milestone_to_parent(task, None, None)
        task.refresh_from_db()
        if (task.milestone_id, task.sprint_id, task.root_task_id) != before:
            cascade_tree_position_to_subtasks(task)
        # A task filed under a milestone's backing row by `parent_task_id`
        # alone has no milestone FK for the CLEAR branch to find (MIP-786).
        if task.parent_task_id is not None:
            parent = (
                TaskMaster.objects.filter(task_id=task.parent_task_id, is_milestone=True)
                .only("task_id", "project_id")
                .first()
            )
            if parent is not None and parent.project_id != task.project_id:
                task.parent_task_id = None
                task.root_task_id = task.task_id
                task.save(update_fields=["parent_task_id", "root_task_id", "ts_updated_at"])
                cascade_tree_position_to_subtasks(task)

        relocate_tasks(
            collect_descendant_task_ids(task.task_id),
            project_id=task.project_id,
            team_id=task.team_id,
        )
        relocate_task_satellites({task.task_id}, project_id=task.project_id, team_id=task.team_id)
        task.refresh_from_db()

    invalidate_for_task(task)
    invalidate_project_tasks_cache(old_team_id, old_project_id)
