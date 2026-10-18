"""`update_task` write tool — Phase 11.

Partial-update one `TaskMaster` row. Any subset of `title`,
`content_text`, `status`, `priority`, `effort_level`, `due_date`,
`milestone_id`, `tags` may be supplied; omitted fields are left as-is. The model should
fetch the task first (via `fetch_task`) so it can propose changes
that respect the existing state.

ACL: same set as `fetch_task` — project members + assignee +
reporter. The requesting user must be on that list to write.

Approval flow (Phase 7): like all write tools, `requires_approval=True`.
The user sees the proposed argument set in the Approve / Reject card
and decides whether to execute.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.blocknote_md import task_body_blocks
from origin.search_engine.agent.tools.body_mentions import notify_task_body_mentions
from origin.search_engine.agent.tools.entity_chips import CHIP_SYNTAX_HINT, chip_resolver
from origin.search_engine.agent.tools.task_tags import resolve_tags, tag_names
from origin.services.collab_push import push_task_body
from origin.services.milestone_service import sync_backing_task
from origin.services.task_milestone_move import milestone_move_error, move_task_to_milestone

# Backing-task columns whose milestone twin has a different name. Every
# other mirrored field shares its name across the two models.
_MILESTONE_FIELD_ALIASES = {"content": "description"}

_VALID_STATUSES = {"Open", "WIP", "Blocked", "Pending", "Closed", "Deleted"}
# Canonical enums live in `frontend/.../taskMeta.ts`. Keep in sync —
# a value outside the set still saves but renders without chip colour.
_VALID_PRIORITIES = {"Minimal", "Low", "Normal", "High", "Critical"}
_VALID_EFFORTS = {"Minimal", "Low", "Moderate", "High", "Extensive"}


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_task_id = args.get("task_id")
    try:
        task_id = int(raw_task_id)
    except (TypeError, ValueError):
        raise ToolError(f"`task_id` must be an integer (got {raw_task_id!r}).")

    try:
        task = TaskMaster.objects.get(task_id=task_id)
    except TaskMaster.DoesNotExist:
        raise ToolError(f"Task {task_id} not found.")
    if task.is_deleted:
        raise ToolError(f"Task {task_id} has been deleted.")

    # Tenant + ACL: identical to the read-side fetch_task. A user who
    # can't see the task can't edit it.
    if str(getattr(task, "team_id", "") or "") != ctx.team_id:
        raise ToolError("Not authorized: task is in a different team.")
    allowed = task_acl_user_ids(
        getattr(task, "project_id", None),
        getattr(task, "assignee_id", None),
        getattr(task, "reporter_id", None),
    )
    if ctx.user_id not in allowed:
        raise ToolError(f"Not authorized to update task {task_id}.")

    update_fields: list[str] = []
    changed: list[str] = []

    # --- title ---
    if "title" in args and args["title"] is not None:
        new_title = (args.get("title") or "").strip()
        if not new_title:
            raise ToolError("`title` must be non-empty if provided.")
        if new_title != task.title:
            task.title = new_title
            update_fields.append("title")
            changed.append("title")

    # --- content_text → BlockNote shape ---
    if "content_text" in args and args["content_text"] is not None:
        new_content = task_body_blocks(
            args.get("content_text") or "",
            entity_chip_resolver=chip_resolver(team_id=ctx.team_id, user_id=ctx.user_id),
        )
        if new_content != task.content:
            task.content = new_content
            update_fields.append("content")
            changed.append("content")

    # --- status ---
    if "status" in args and args["status"] is not None:
        new_status = args["status"]
        if new_status not in _VALID_STATUSES:
            raise ToolError(
                f"`status` must be one of {sorted(_VALID_STATUSES)} (got {new_status!r})."
            )
        if new_status != task.status:
            task.status = new_status
            update_fields.append("status")
            changed.append("status")

    # --- priority ---
    if "priority" in args and args["priority"] is not None:
        new_priority = args["priority"]
        if new_priority not in _VALID_PRIORITIES:
            raise ToolError(
                f"`priority` must be one of {sorted(_VALID_PRIORITIES)} (got {new_priority!r})."
            )
        if new_priority != task.priority:
            task.priority = new_priority
            update_fields.append("priority")
            changed.append("priority")

    # --- effort_level ---
    if "effort_level" in args and args["effort_level"] is not None:
        new_effort = args["effort_level"]
        if new_effort not in _VALID_EFFORTS:
            raise ToolError(
                f"`effort_level` must be one of {sorted(_VALID_EFFORTS)} (got {new_effort!r})."
            )
        if new_effort != task.effort_level:
            task.effort_level = new_effort
            update_fields.append("effort_level")
            changed.append("effort_level")

    # --- due_date (ISO 8601 date string, or empty string to clear) ---
    if "due_date" in args and args["due_date"] is not None:
        raw_due = args["due_date"]
        if raw_due == "":
            if task.due_date is not None:
                task.due_date = None
                update_fields.append("due_date")
                changed.append("due_date(cleared)")
        else:
            try:
                new_due = date.fromisoformat(str(raw_due))
            except (TypeError, ValueError):
                raise ToolError(
                    f"`due_date` must be an ISO 8601 date (got {raw_due!r}). Use '' to clear."
                )
            if new_due != task.due_date:
                task.due_date = new_due
                update_fields.append("due_date")
                changed.append("due_date")

    # --- tags (names from the project's catalogue; [] clears) ---
    # Compared as name sets: the UI keeps no tag order, and the history
    # signal ignores a reorder too. The save records `tags_changed`.
    if "tags" in args and args["tags"] is not None:
        new_tags = resolve_tags(args["tags"], task.project_id)
        if set(tag_names(new_tags)) != set(tag_names(task.tags)):
            task.tags = new_tags or None
            update_fields.append("tags")
            changed.append("tags" if new_tags else "tags(cleared)")

    # --- milestone_id (move into / between milestones, '' to detach) ---
    # Validated here, before any write, but applied after the field save:
    # the move re-reads the row, and writes the hierarchy columns itself.
    move_milestone = "milestone_id" in args and args["milestone_id"] is not None
    target_milestone_id: int | None = None
    if move_milestone:
        raw_milestone = args["milestone_id"]
        if raw_milestone != "":
            try:
                target_milestone_id = int(raw_milestone)
            except (TypeError, ValueError):
                raise ToolError(
                    f"`milestone_id` must be an integer (got {raw_milestone!r}). Use '' to detach."
                )
        if error := milestone_move_error(task, target_milestone_id, ctx.team_id):
            raise ToolError(error)

    if not update_fields and not move_milestone:
        return {
            "task_id": task_id,
            "changed_fields": [],
            "__summary__": f"No changes applied to task {task.display_id}.",
        }

    try:
        milestone = (
            MilestoneMaster.objects.filter(task_id=task.task_id, is_deleted=False).first()
            if task.is_milestone
            else None
        )
        if not update_fields:
            pass  # milestone move only — applied below
        elif milestone is not None:
            # A milestone is a task with `is_milestone=True`, but its
            # AUTHORITATIVE values live on the MilestoneMaster side table;
            # the backing TaskMaster row is a mirror `sync_backing_task`
            # overwrites on the next milestone PATCH. Writing only the
            # backing row (the plain `task.save` below) made the table
            # show the new status while the preview kept the old one, and
            # the next milestone edit silently reverted the backing row —
            # data loss. So mirror the same changed fields onto the
            # milestone and let `sync_backing_task` push them back down.
            #
            # The values are already assigned to `task`; copy them across
            # by field name (aliasing the two columns that differ) rather
            # than re-parsing the args.
            for field in update_fields:
                milestone_field = _MILESTONE_FIELD_ALIASES.get(field, field)
                setattr(milestone, milestone_field, getattr(task, field))
            milestone.save()
            sync_backing_task(milestone)
            # `sync_backing_task` re-derived and saved the backing row from
            # the milestone, so the `task` instance mutated above is stale
            # — re-read it for the response payload.
            task.refresh_from_db()
        else:
            # `ts_updated_at` is auto_now, but Django only runs pre_save()
            # on the fields named in update_fields — omit it and the
            # timestamp silently stays put. It's the incremental
            # reindexer's watermark (task_chunker filters
            # ts_updated_at__gte) and list_tasks' sort key, so a status
            # change that doesn't move it never reaches search. The REST
            # path lists it explicitly for the same reason. (The milestone
            # branch above saves the backing row wholesale, which bumps
            # every auto_now field, so it needs no explicit list.)
            task.save(update_fields=[*update_fields, "ts_updated_at"])
        # The same bridge + sub-tree cascade the milestone picker runs, so
        # the task nests under the milestone row exactly as if a user had
        # moved it (MIP-692).
        if move_milestone and move_task_to_milestone(task, target_milestone_id):
            changed.append("milestone" if target_milestone_id is not None else "milestone(cleared)")
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Failed to update task: {e}")

    # The editor renders the task's Yjs doc, not `content`; without this
    # push a task that was ever opened kept its old body on screen, and the
    # next keystroke saved the old body back (MIP-694). After the save, and
    # from the re-read row, so a milestone pushes what sync_backing_task
    # actually stored.
    if "content" in update_fields:
        push_task_body(task.task_id, task.content or [])
        # `@` chips ping their people, diffed against the stored set so
        # a re-save of the same body pings no one twice (MIP-708).
        notify_task_body_mentions(task, actor_id=ctx.user_id)

    if not changed:
        return {
            "task_id": task_id,
            "changed_fields": [],
            "__summary__": f"No changes applied to task {task.display_id}.",
        }
    return {
        "task_id": task_id,
        "changed_fields": changed,
        "status": task.status,
        "title": task.title,
        "milestone_id": task.milestone_id,
        "__summary__": f"Updated task {task.display_id}: {', '.join(changed)}",
    }


UPDATE_TASK = Tool(
    name="update_task",
    description=(
        "Update one or more fields of an existing task. REQUIRES USER "
        "APPROVAL — the user sees your proposed changes and decides "
        "whether to execute. Required: task_id. Optional (omit fields "
        "you don't want to change): title, content_text, status, "
        "priority, effort_level, due_date, milestone_id, tags. Pass `due_date: ''` to "
        "clear the due date. Pass `milestone_id` (from `list_milestones`, same "
        "project) to move the task and its sub-tasks into that milestone, or "
        "`milestone_id: ''` to take it out. Status enum: Open, WIP, Blocked, Pending, Closed, "
        "Deleted. `tags` REPLACES the task's tags with names from "
        "list_project_tags (pass [] to clear); an unknown name is refused. "
        "Use `fetch_task` first to see the current state so "
        "you don't propose a no-op."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "INTEGER",
                "description": "Numeric task id to update.",
            },
            "title": {
                "type": "STRING",
                "description": "New title (1 line). Omit to leave unchanged.",
            },
            "content_text": {
                "type": "STRING",
                "description": (
                    "Replace the task body with this markdown. Omit to leave "
                    "unchanged. Pass '' to clear the body." + CHIP_SYNTAX_HINT
                ),
            },
            "status": {
                "type": "STRING",
                "enum": ["Open", "WIP", "Blocked", "Pending", "Closed", "Deleted"],
                "description": "New status. Omit to leave unchanged.",
            },
            "priority": {
                "type": "STRING",
                "enum": ["Minimal", "Low", "Normal", "High", "Critical"],
                "description": "New priority. Omit to leave unchanged.",
            },
            "effort_level": {
                "type": "STRING",
                "enum": ["Minimal", "Low", "Moderate", "High", "Extensive"],
                "description": "New effort estimate. Omit to leave unchanged.",
            },
            "due_date": {
                "type": "STRING",
                "description": (
                    "New ISO 8601 date (e.g. 2026-06-30). Omit to leave "
                    "unchanged. Pass '' to clear."
                ),
            },
            "milestone_id": {
                "type": "STRING",
                "description": (
                    "Milestone id (from list_milestones, same project) to move "
                    "the task into; its sub-tasks move with it. Omit to leave "
                    "unchanged. Pass '' to take the task out of its milestone."
                ),
            },
            "tags": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": (
                    "The task's full new set of tag names, from list_project_tags. "
                    "Replaces the current tags — include any you are keeping. "
                    "Pass [] to clear. Omit to leave unchanged."
                ),
            },
        },
        "required": ["task_id"],
    },
    run=_run,
    requires_approval=True,
)
