"""`update_task` write tool — Phase 11.

Partial-update one `TaskMaster` row. Any subset of `title`,
`content_text`, `status`, `priority`, `effort_level`, `due_date`,
`milestone_id`, `parent_task_id`, `project_id`, `tags`, `start_date`, `collaborators`,
`custom_fields` may be supplied; omitted fields are left as-is. The model should
fetch the task first (via `fetch_task`) so it can propose changes
that respect the existing state.

ACL: same set as `fetch_task` — project members + assignee +
reporter. The requesting user must be on that list to write.

Approval flow (Phase 7): like all write tools, `requires_approval=True`.
The user sees the proposed argument set in the Approve / Reject card
and decides whether to execute.

MIP-1561 (flag `AGENT_PARITY_TOOLS`, default OFF; with it off neither exists, see
`parity.ignore_unless_parity`): `links` replaces the task's links as the app's task page saves them
(`task_links`), and `reporter_id` changes the reporter under `assign_task`'s rule (a member of the
team, or someone the task's own ACL already covers; a reporter already on the task is not judged
again). Neither is something undo can put back (`undo.UPDATE_TASK_UNRESTORABLE_ARGS`).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.db import transaction

from apis.request_id import log_failure
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.chip_syntax import CHIP_SYNTAX_HINT
from origin.search_engine.agent.parity import ignore_unless_parity
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.blocknote_md import task_body_blocks
from origin.search_engine.agent.tools.body_limits import check_body_size
from origin.search_engine.agent.tools.body_mentions import notify_task_body_mentions
from origin.search_engine.agent.tools.entity_chips import chip_resolver
from origin.search_engine.agent.tools.task_body_edit import (
    append_blocks,
    body_hash,
    prepend_blocks,
    replace_in_body,
)
from origin.search_engine.agent.tools.task_custom_fields import (
    CUSTOM_FIELDS_SCHEMA,
    parse_iso_date,
    plan_custom_fields,
    project_fields,
)
from origin.search_engine.agent.tools.task_links import (
    LINKS_SCHEMA,
    plan_links,
    register_pr_webhooks,
)
from origin.search_engine.agent.tools.task_parent import require_readable_parent
from origin.search_engine.agent.tools.task_people import plan_collaborators
from origin.search_engine.agent.tools.task_tags import resolve_tags, tag_names
from origin.search_engine.agent.undo import UNDO_KEY, TaskUpdateCapture
from origin.services.collab_push import push_task_body
from origin.services.milestone_service import sync_backing_task
from origin.services.task_collaborators import sync_task_collaborators
from origin.services.task_milestone_move import milestone_move_error, move_task_to_milestone
from origin.services.task_reparent import (
    move_task_to_parent,
    move_task_to_project,
    parent_move_error,
    project_move_error,
)
from origin.views.utils.scope_guards import is_team_member

# Backing-task columns whose milestone twin has a different name. Every
# other mirrored field shares its name across the two models.
_MILESTONE_FIELD_ALIASES = {"content": "description"}

_VALID_STATUSES = {"Open", "WIP", "Blocked", "Pending", "Closed", "Deleted"}
# Canonical enums live in `frontend/.../taskMeta.ts`. Keep in sync —
# a value outside the set still saves but renders without chip colour.
_VALID_PRIORITIES = {"Minimal", "Low", "Normal", "High", "Critical"}
_VALID_EFFORTS = {"Minimal", "Low", "Moderate", "High", "Extensive"}


def _new_reporter(raw: Any, task: TaskMaster, allowed: set[str], ctx: ToolContext) -> str | None:
    """The reporter id to save, or None when `raw` is the reporter already (so a call that
    restates the current one changes nothing and is not judged again). The guard is
    `assign_task`'s: a member of this team, or someone the task's ACL already covers; both sets
    are scoped to this team, so a UUID from another tenant is refused in the words of a missing
    one. A task always has a reporter, so there is no clearing."""
    if raw in ("", "null"):
        raise ToolError("`reporter_id` must be a user id: a task always has a reporter.")
    if str(raw) == str(task.reporter_id):
        return None
    if not (str(raw) in allowed or is_team_member(ctx.team_id, raw)):
        raise ToolError(
            f"User {raw!r} is not an active member of this team, and is not on this task's "
            "project. Use get_team_members or list_project_members to find valid user ids."
        )
    try:
        person = CustomUser.objects.get(id=raw)
    except (CustomUser.DoesNotExist, Exception):
        raise ToolError(f"User {raw!r} not found.")
    if person.is_deleted:
        raise ToolError(f"User {raw!r} account has been deleted.")
    return str(person.id)


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    args = ignore_unless_parity(args, PARITY_PROPERTIES)
    check_body_size(args.get("content_text"), "content_text")
    check_body_size(args.get("body_append"), "body_append")
    check_body_size(args.get("body_prepend"), "body_prepend")
    body_patch = args.get("body_replace")
    if isinstance(body_patch, dict):
        check_body_size(body_patch.get("old_text"), "body_replace.old_text")
        check_body_size(body_patch.get("new_text"), "body_replace.new_text")
    raw_task_id = args.get("task_id")
    try:
        task_id = int(raw_task_id)
    except (TypeError, ValueError):
        raise ToolError(f"`task_id` must be an integer (got {raw_task_id!r}).")

    try:
        task = TaskMaster.objects.get(task_id=task_id)
    except TaskMaster.DoesNotExist:
        raise ToolError(f"Task {task_id} not found.")
    if str(getattr(task, "team_id", "") or "") != ctx.team_id:
        # The words of a missing one, so a guessed id does not say which tasks exist in other
        # teams (MIP-1531); the class still counts toward the MCP refusal alarm.
        raise ToolForbidden(f"Task {task_id} not found.")
    if task.is_deleted:
        raise ToolError(f"Task {task_id} has been deleted.")

    # Tenant + ACL: identical to the read-side fetch_task. A user who
    # can't see the task can't edit it.
    allowed = task_acl_user_ids(
        getattr(task, "project_id", None),
        getattr(task, "assignee_id", None),
        getattr(task, "reporter_id", None),
    )
    if ctx.user_id not in allowed:
        raise ToolForbidden(f"Not authorized to update task {task_id}.")

    # A milestone's backing row is a task, so `status: "Deleted"` would be
    # accepted and mirrored onto the milestone, but that is only HALF of a
    # delete. `MilestoneView.delete` also sets `is_deleted`, detaches the
    # milestone's tasks and purges the search index; none of that happens
    # here, so the milestone stayed in `list_milestones` as "Deleted" with
    # its tasks still attached (MIP-876). Refuse rather than half-delete: a
    # real milestone delete is destructive and belongs behind its own tool
    # (MIP-879). `update_tasks_bulk` already refuses Deleted outright.
    if task.is_milestone and args.get("status") == "Deleted":
        raise ToolError(
            f"{task.display_id} is a milestone, and update_task can't delete one: "
            "it would stay listed as Deleted with its tasks still attached. "
            "Nothing was changed. Use `delete_milestone` to delete it."
        )

    display_id_before = task.display_id
    # Undo (`agent/undo.py`): the values this call is about to replace, taken
    # before the first one changes. A call that touches what undo cannot put
    # back (collaborators, custom fields, a move) captures nothing.
    undo_capture = TaskUpdateCapture(task, args) if ctx.capture_undo else None

    # --- body guards (MIP-824): validated before anything is touched ---
    edit_modes = [
        key
        for key in ("content_text", "body_append", "body_prepend", "body_replace")
        if args.get(key) is not None
    ]
    if len(edit_modes) > 1:
        raise ToolError(
            f"Send only one of content_text, body_append, body_prepend, body_replace "
            f"(got {', '.join(edit_modes)}). Nothing was changed."
        )
    base_hash = args.get("base_body_hash")
    if base_hash is not None:
        # The hash `fetch_task` returned. Refuses the whole call, so a stale
        # caller changes nothing (not even a status sent alongside).
        if not isinstance(base_hash, str) or not base_hash.strip():
            raise ToolError("`base_body_hash` must be the `body_hash` string from fetch_task.")
        current_hash = body_hash(task.content)
        if base_hash.strip() != current_hash:
            raise ToolError(
                "The task body changed since you read it (`base_body_hash` is stale). "
                "Read the task again and redo the edit against the current body. "
                "Nothing was changed."
            )

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
    # --- partial body edits (MIP-824): splice into the stored blocks ---
    new_content: list[dict[str, Any]] | None = None
    if "content_text" in args and args["content_text"] is not None:
        new_content = task_body_blocks(
            args.get("content_text") or "",
            entity_chip_resolver=chip_resolver(team_id=ctx.team_id, user_id=ctx.user_id),
        )
    elif edit_modes:
        resolver = chip_resolver(team_id=ctx.team_id, user_id=ctx.user_id)
        mode = edit_modes[0]
        if mode == "body_append":
            new_content = append_blocks(task.content, args["body_append"], resolver)
        elif mode == "body_prepend":
            new_content = prepend_blocks(task.content, args["body_prepend"], resolver)
        else:
            patch = args["body_replace"]
            if not isinstance(patch, dict):
                raise ToolError("`body_replace` must be an object {old_text, new_text}.")
            new_content = replace_in_body(
                task.content, patch.get("old_text"), patch.get("new_text"), resolver
            )
    if new_content is not None:
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

    # --- start_date (ISO 8601 date, or empty string to clear) ---
    # The UI checks nothing against `due_date` (no min or max on either picker,
    # and the PUT does not compare them), so neither does this.
    if "start_date" in args and args["start_date"] is not None:
        raw_start = args["start_date"]
        if raw_start == "":
            if task.start_date is not None:
                task.start_date = None
                update_fields.append("start_date")
                changed.append("start_date(cleared)")
        else:
            new_start = parse_iso_date(raw_start, "`start_date`")
            if new_start != task.start_date:
                task.start_date = new_start
                update_fields.append("start_date")
                changed.append("start_date")

    # --- reporter_id and links (MIP-1561, `AGENT_PARITY_TOOLS`) ---
    # The links replace the whole set, as the task page's save does; the reporter is judged
    # only when it changes. Neither rides along with a project move: the reporter is judged
    # against the project the task is in now, and the links are the task's own.
    new_links: list[dict[str, Any]] | None = None
    if args.get("reporter_id") is not None:
        new_reporter = _new_reporter(args["reporter_id"], task, allowed, ctx)
        if new_reporter is not None:
            task.reporter_id = new_reporter
            update_fields.append("reporter_id")
            changed.append("reporter")
    if args.get("links") is not None:
        planned_links = plan_links(args["links"], task.links)
        if planned_links != (task.links if isinstance(task.links, list) else []):
            new_links = planned_links
            task.links = planned_links
            update_fields.append("links")
            changed.append("links" if planned_links else "links(cleared)")

    # --- custom_fields (by name) and collaborators ---
    # Planned here, written below with the rest. Both belong to the project the
    # task is in, so neither rides along with a project move.
    new_custom_values: dict[str, Any] | None = None
    new_collaborators: list[str] | None = None
    wants_custom = "custom_fields" in args and args["custom_fields"] is not None
    wants_collaborators = "collaborators" in args and args["collaborators"] is not None
    if (wants_custom or wants_collaborators) and args.get("project_id") is not None:
        raise ToolError(
            "`custom_fields` and `collaborators` belong to the project the task is in: "
            "move the task with `project_id` first, then set them in a second call. "
            "Nothing was changed."
        )
    if wants_custom:
        # The project's fields are for its members (the REST list is member-gated,
        # and `get_task` shows them only to a member). A task's assignee or
        # reporter can write the task without being one, and a refusal that
        # listed the field names, or said which names exist, would hand them over.
        if not ProjectMembers.objects.filter(
            project_id=task.project_id, attendee_id=ctx.user_id
        ).exists():
            raise ToolError(
                "`custom_fields`: only a member of the task's project can set its custom "
                "fields. Nothing was changed."
            )
        planned = plan_custom_fields(
            args["custom_fields"],
            project_fields(task.project_id),
            task.custom_field_values,
            task.project,
        )
        if planned != (task.custom_field_values or {}):
            new_custom_values = planned
            task.custom_field_values = planned
            update_fields.append("custom_field_values")
            changed.append("custom_fields")
    if wants_collaborators:
        current_people = {str(uid) for uid in task.collaborators.values_list("id", flat=True)}
        planned_people = plan_collaborators(args["collaborators"], task.project, current_people)
        if set(planned_people) != current_people:
            new_collaborators = planned_people
            changed.append("collaborators" if planned_people else "collaborators(cleared)")

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

    # --- parent_task_id (hang under another task, '' for top-level) and
    # project_id (move to another project) ---
    # Validated here, before any write, and applied after the field save for
    # the same reason as the milestone move. Each one places the task in the
    # hierarchy, and so does `milestone_id`: they are separate calls, or the
    # order they apply in would decide where the task ends up (MIP-899).
    move_parent = "parent_task_id" in args and args["parent_task_id"] is not None
    move_project = "project_id" in args and args["project_id"] is not None
    target_parent_id: int | None = None
    target_project_id: int | None = None
    if move_parent:
        raw_parent = args["parent_task_id"]
        if raw_parent != "":
            try:
                target_parent_id = int(raw_parent)
            except (TypeError, ValueError):
                raise ToolError(
                    f"`parent_task_id` must be a task id (got {raw_parent!r}). "
                    "Use '' to make the task top-level."
                )
        if target_parent_id is not None:
            require_readable_parent(target_parent_id, ctx)
        if target_parent_id == task.parent_task_id:
            move_parent = False  # already there
        elif error := parent_move_error(task, target_parent_id, ctx.team_id):
            raise ToolError(error)
    if move_project:
        try:
            target_project_id = int(args["project_id"])
        except (TypeError, ValueError):
            raise ToolError(f"`project_id` must be an integer (got {args['project_id']!r}).")
        if target_project_id == task.project_id:
            move_project = False  # already there
        elif move_parent and target_parent_id is not None:
            raise ToolError(
                "Move the project and set a parent in two calls: the parent has to be in "
                "the task's project. Pass `parent_task_id: ''` with `project_id` to take "
                "the task out from under its parent while it moves."
            )
        elif error := project_move_error(
            task,
            target_project_id,
            ctx.team_id,
            ctx.user_id,
            detaching=move_parent and target_parent_id is None,
        ):
            raise ToolError(error)
    if move_milestone and (move_parent or move_project):
        raise ToolError(
            "`milestone_id`, `parent_task_id` and `project_id` each decide where the task "
            "sits; send one of them per call. Nothing was changed."
        )

    if (
        not update_fields
        and new_collaborators is None
        and not move_milestone
        and not move_parent
        and not move_project
    ):
        return {
            "task_id": task_id,
            "changed_fields": [],
            **({"body_hash": body_hash(task.content)} if (edit_modes or base_hash) else {}),
            "__summary__": f"No changes applied to task {task.display_id}.",
        }

    try:
        milestone = (
            MilestoneMaster.objects.filter(task_id=task.task_id, is_deleted=False).first()
            if task.is_milestone
            else None
        )
        if not update_fields:
            pass  # milestone move or collaborators only — applied below
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
            #
            # Custom values are the exception: they live on the backing row
            # only (`MilestoneView.patch` writes them there, after the sync, and
            # the sync never touches the column), so they are saved to it below.
            for field in update_fields:
                if field == "custom_field_values":
                    continue
                milestone_field = _MILESTONE_FIELD_ALIASES.get(field, field)
                setattr(milestone, milestone_field, getattr(task, field))
            if update_fields != ["custom_field_values"]:
                milestone.save()
                sync_backing_task(milestone)
                # `sync_backing_task` re-derived and saved the backing row from
                # the milestone, so the `task` instance mutated above is stale
                # — re-read it for the response payload.
                task.refresh_from_db()
            if new_custom_values is not None:
                task.custom_field_values = new_custom_values
                task.save(update_fields=["custom_field_values", "ts_updated_at"])
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
        # The M2M is replaced wholesale, as the PUT does. It touches no column,
        # so `ts_updated_at` (the incremental reindexer's and the client sync's
        # watermark, and `list_tasks`' sort key) is bumped by hand; the PUT's
        # full save bumps it on every edit.
        if new_collaborators is not None:
            with transaction.atomic():
                sync_task_collaborators(task, new_collaborators)
                task.save(update_fields=["ts_updated_at"])
        # The same bridge + sub-tree cascade the milestone picker runs, so
        # the task nests under the milestone row exactly as if a user had
        # moved it (MIP-692).
        if move_milestone and move_task_to_milestone(task, target_milestone_id):
            changed.append("milestone" if target_milestone_id is not None else "milestone(cleared)")
        # Parent, then project: with both, the parent edge is cut first
        # (validated above), which is what lets a nested task move.
        if move_parent or move_project:
            with transaction.atomic():
                if move_parent and move_task_to_parent(task, target_parent_id):
                    changed.append("parent" if target_parent_id is not None else "parent(cleared)")
                if move_project:
                    move_task_to_project(task, target_project_id)
                    changed.append("project")
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        # The model and the MCP caller read this; the database error
        # goes to the log with a reference (MIP-622).
        ref = log_failure("update_task tool failed", e)
        raise ToolError(f"Failed to update task (ref {ref}).")

    # The editor renders the task's Yjs doc, not `content`; without this
    # push a task that was ever opened kept its old body on screen, and the
    # next keystroke saved the old body back (MIP-694). After the save, and
    # from the re-read row, so a milestone pushes what sync_backing_task
    # actually stored.
    if new_links:
        # As the app's save does: a GitHub PR link gets the repo's webhook, best effort.
        register_pr_webhooks(ctx.user_id, new_links)

    if "content" in update_fields:
        push_task_body(task.task_id, task.content or [])
        # `@` chips ping their people, diffed against the stored set so
        # a re-save of the same body pings no one twice (MIP-708).
        notify_task_body_mentions(task, actor_id=ctx.user_id)

    # A caller chaining edits needs the hash of what it just wrote, without
    # another read. Taken from the row as stored (a milestone's backing row
    # was re-derived by `sync_backing_task`).
    hash_field = {"body_hash": body_hash(task.content)} if (edit_modes or base_hash) else {}
    if not changed:
        return {
            "task_id": task_id,
            "changed_fields": [],
            **hash_field,
            "__summary__": f"No changes applied to task {task.display_id}.",
        }
    result = {
        "task_id": task_id,
        "changed_fields": changed,
        "status": task.status,
        "title": task.title,
        "display_id": task.display_id,
        "milestone_id": task.milestone_id,
        "parent_task_id": task.parent_task_id,
        "project_id": task.project_id,
        **hash_field,
        "__summary__": (
            f"Updated task {task.display_id}: {', '.join(changed)}"
            + (f" (was {display_id_before})" if task.display_id != display_id_before else "")
        ),
    }
    if undo_capture is not None and (image := undo_capture.finish(update_fields)):
        result[UNDO_KEY] = image
    return result


#: What `AGENT_PARITY_TOOLS` adds to this tool's schema (`tool_flag_params`); the tool takes
#: these only while the flag is on (`parity.ignore_unless_parity`).
PARITY_PROPERTIES: dict[str, dict[str, Any]] = {
    "links": {
        **LINKS_SCHEMA,
        "description": (
            "The task's full new set of links, [{url, title}] (http(s) only). Replaces the "
            "current links: include any you keep, as get_task shows them. [] clears. Omit to "
            "leave unchanged."
        ),
    },
    "reporter_id": {
        "type": "STRING",
        "description": (
            "New reporter's user id (get_team_members / list_project_members). Same rule as "
            "assign_task. Omit to leave unchanged."
        ),
    },
}

#: The sentence the flag adds to the description (`tool_flag_text`).
DESCRIPTION_ADDITION = (
    " `links` replaces the task's links ([] clears); `reporter_id` changes the reporter."
)

UPDATE_TASK = Tool(
    name="update_task",
    description=(
        "Update one or more fields of an existing task. REQUIRES USER "
        "APPROVAL — the user sees your proposed changes and decides "
        "whether to execute. Required: task_id. Optional (omit fields "
        "you don't want to change): title, content_text, status, "
        "priority, effort_level, due_date, milestone_id, parent_task_id, project_id, tags. To change "
        "part of the body, send ONE of body_append, body_prepend or "
        "body_replace instead of content_text (which rewrites all of it); "
        "pass base_body_hash (the body_hash fetch_task returned) to refuse "
        "the edit if the body changed since you read it. Pass `due_date: ''` to "
        "clear the due date. Pass `milestone_id` (from `list_milestones`, same "
        "project) to move the task and its sub-tasks into that milestone, or "
        "`milestone_id: ''` to take it out. `parent_task_id` re-parents the "
        "task within its project ('' makes it top-level) and `project_id` "
        "moves it, with its sub-tasks, to another project; send only one of "
        "milestone_id, parent_task_id and project_id per call. Status enum: Open, WIP, Blocked, Pending, Closed, "
        "Deleted. `tags` REPLACES the task's tags with names from "
        "list_project_tags (pass [] to clear); an unknown name is refused. "
        "`start_date` sets the planned start, `collaborators` replaces the "
        "collaborators, and `custom_fields` sets the project's custom fields by "
        "name (list_project_tags lists them); send the last two apart from "
        "`project_id`. '' or [] clears. "
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
            "body_append": {
                "type": "STRING",
                "description": (
                    "Markdown added after the end of the existing body, which is "
                    "left as it is. Instead of content_text; send only one of "
                    "content_text, body_append, body_prepend, body_replace." + CHIP_SYNTAX_HINT
                ),
            },
            "body_prepend": {
                "type": "STRING",
                "description": (
                    "Markdown added before the start of the existing body, which "
                    "is left as it is. Instead of content_text." + CHIP_SYNTAX_HINT
                ),
            },
            "body_replace": {
                "type": "OBJECT",
                "description": (
                    "Replace one passage of the body. `old_text` must appear "
                    "exactly once in the task's `content_markdown` (copy it from "
                    "there); zero or several matches are refused. Instead of "
                    "content_text."
                ),
                "properties": {
                    "old_text": {
                        "type": "STRING",
                        "description": "The markdown to find, exactly as in content_markdown.",
                    },
                    "new_text": {
                        "type": "STRING",
                        "description": "What to put in its place. '' deletes the match.",
                    },
                },
                "required": ["old_text", "new_text"],
            },
            "base_body_hash": {
                "type": "STRING",
                "description": (
                    "The `body_hash` that fetch_task / get_task returned when you "
                    "read the task. If the body has changed since, the whole call "
                    "is refused and nothing is written. Omit to skip the check."
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
            "parent_task_id": {
                "type": "STRING",
                "description": (
                    "Task id to hang this task under, in the same project; its "
                    "sub-tasks move with it and it joins that task's milestone. "
                    "Omit to leave unchanged. Pass '' to make it top-level, which "
                    "also takes it out of its milestone."
                ),
            },
            "project_id": {
                "type": "INTEGER",
                "description": (
                    "Project id (from list_projects) to move the task and its "
                    "sub-tasks to. They are renumbered there, so the display id "
                    "changes, and tags and milestone are cleared. A sub-task "
                    "follows its parent: pass parent_task_id '' with it. Omit to "
                    "leave unchanged."
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
            "start_date": {
                "type": "STRING",
                "description": (
                    "New planned start, ISO 8601 (YYYY-MM-DD). Omit to leave "
                    "unchanged. Pass '' to clear."
                ),
            },
            "collaborators": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": (
                    "The task's full new set of collaborator user ids "
                    "(get_team_members / list_project_members). Replaces the current "
                    "set: include any you are keeping. New ones must be on the team "
                    "or project. Pass [] to clear. Omit to leave unchanged."
                ),
            },
            "custom_fields": CUSTOM_FIELDS_SCHEMA,
        },
        "required": ["task_id"],
    },
    run=_run,
    requires_approval=True,
)
