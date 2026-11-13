"""`update_tasks_bulk` composite write tool — organize many tasks in ONE approval.

The tool behind "reprioritize / organize / clean up the tasks in this
milestone": the agent reads current state (`list_tasks` +
`list_task_dependencies`), then proposes per-task priority / due-date /
status / effort changes — each carrying a one-sentence `rationale` the
user reads in the approval card — and ONE approval applies all of them.
Without it, a 10-task organize pass is ten `update_task` proposals and
ten Approve clicks.

Semantics: **validate-all-then-apply-all, atomic.** The user approved a
specific plan; a partially-applied one is worse than a clean failure
the model can re-propose. Phase 1 resolves every task in one query and
checks existence / tenant / ACL / enums / dates — any problem fails the
whole batch with every offending `updates[i]` named. Phase 2 applies
the field diffs (`update_task`'s skip-no-op semantics) inside one
transaction. State can drift between the pause and the approval —
that's why validation runs at approve time, not proposal time.

`rationale` is never persisted onto task rows — it lives in the run's
`arguments_json` (audit trail) and the approval preview only.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from django.db import transaction

from apis.request_id import log_failure
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.task_custom_fields import parse_iso_date
from origin.search_engine.agent.tools.task_enums import (
    EFFORT_ENUM,
    PRIORITY_ENUM,
    STATUS_ENUM,
    VALID_EFFORTS,
    VALID_PRIORITIES,
    VALID_STATUSES,
)
from origin.search_engine.agent.tools.task_tags import resolve_tags, tag_names
from origin.services.milestone_service import sync_backing_task
from origin.services.task_assignment_notice import notify_task_assigned
from origin.services.task_cache import invalidate_project_tasks_cache
from origin.services.task_milestone_move import milestone_move_error, move_task_to_milestone
from origin.views.utils.scope_guards import is_team_member

_MAX_BULK_UPDATES = 30
# `changes` key for a milestone move; not a TaskMaster column, so the
# field-diff loop never sees it.
_MILESTONE_KEY = "milestone_id"
# Fields an update row may change, in apply order.
# Column an update row may set from a user id (MIP-823); '' clears it.
_ASSIGNEE_KEY = "assignee_id"
_FIELD_SPECS = (
    ("priority", VALID_PRIORITIES),
    ("effort_level", VALID_EFFORTS),
    ("status", VALID_STATUSES),
)


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    updates = args.get("updates")
    if not isinstance(updates, list) or not updates:
        raise ToolError("`updates` must be a non-empty array.")
    if len(updates) > _MAX_BULK_UPDATES:
        raise ToolError(
            f"Too many updates: {len(updates)} (max {_MAX_BULK_UPDATES}). "
            "Split the batch and tell the user."
        )

    # ---- phase 1: resolve + validate EVERYTHING before any write ----
    errors: list[str] = []
    task_ids: list[int] = []
    seen_ids: set[int] = set()
    for i, row in enumerate(updates):
        where = f"updates[{i}]"
        if not isinstance(row, dict):
            errors.append(f"{where}: must be an object.")
            continue
        try:
            tid = int(row.get("task_id"))
        except (TypeError, ValueError):
            errors.append(f"{where}: `task_id` must be an integer (got {row.get('task_id')!r}).")
            continue
        if tid in seen_ids:
            errors.append(f"{where}: duplicate task_id {tid}.")
            continue
        seen_ids.add(tid)
        task_ids.append(tid)

    tasks_by_id = {
        t.task_id: t
        for t in TaskMaster.objects.select_related("project").filter(task_id__in=task_ids)
    }

    # Set-based ACL, mirroring `list_tasks`: one membership query for the
    # requester instead of a per-task `task_acl_user_ids` call.
    member_project_ids = set(
        ProjectMembers.objects.filter(
            attendee_id=ctx.user_id,
            project__team_id=ctx.team_id,
            project__is_deleted=False,
        ).values_list("project_id", flat=True)
    )

    def _may_edit(task: TaskMaster) -> bool:
        return (
            task.project_id in member_project_ids
            or str(task.assignee_id or "") == ctx.user_id
            or str(task.reporter_id or "") == ctx.user_id
        )

    # Who may be assigned, per project, and each looked-up user (MIP-823):
    # the same two-way guard as `assign_task` / `create_task` — a teammate OR
    # someone already on the task's project (a guest has no team row).
    project_members_cache: dict[int | None, set[str]] = {}
    users_cache: dict[str, CustomUser | None] = {}

    def _assignee_error(task: TaskMaster, raw: Any) -> tuple[str | None, str | None]:
        """`(normalised id, None)` when `raw` may be assigned to `task`,
        else `(None, why)`."""
        try:
            uid = str(uuid.UUID(str(raw)))
        except ValueError:
            return None, (
                f"`assignee_id` must be a user id (got {raw!r}). Use get_team_members, "
                "list_project_members or get_current_user. '' unassigns."
            )
        if task.project_id not in project_members_cache:
            project_members_cache[task.project_id] = {
                str(a)
                for a in ProjectMembers.objects.filter(project_id=task.project_id).values_list(
                    "attendee_id", flat=True
                )
                if a
            }
        on_task = {str(task.assignee_id or ""), str(task.reporter_id or "")}
        if not (
            uid in project_members_cache[task.project_id]
            or uid in on_task
            or is_team_member(ctx.team_id, uid)
        ):
            return None, (
                f"User {uid!r} is not an active member of this team, and is not on this "
                "task's project. Use get_team_members or list_project_members to find "
                "valid user ids."
            )
        if uid not in users_cache:
            users_cache[uid] = CustomUser.objects.filter(id=uid).first()
        user = users_cache[uid]
        if user is None:
            return None, f"User {uid!r} not found."
        if user.is_deleted:
            return None, f"User {uid!r} account has been deleted."
        return uid, None

    planned: list[tuple[TaskMaster, dict[str, Any], str]] = []
    for i, row in enumerate(updates):
        if not isinstance(row, dict):
            continue
        where = f"updates[{i}]"
        try:
            tid = int(row.get("task_id"))
        except (TypeError, ValueError):
            continue
        task = tasks_by_id.get(tid)
        if task is None:
            errors.append(f"{where}: task {tid} not found.")
            continue
        if task.is_deleted:
            errors.append(f"{where}: task {tid} has been deleted.")
            continue
        if str(getattr(task, "team_id", "") or "") != ctx.team_id:
            errors.append(f"{where}: not authorized — task {tid} is in a different team.")
            continue
        if not _may_edit(task):
            errors.append(f"{where}: not authorized to update task {tid}.")
            continue

        changes: dict[str, Any] = {}
        for field, valid in _FIELD_SPECS:
            value = row.get(field)
            if value is None:
                continue
            if value not in valid:
                errors.append(f"{where}: `{field}` must be one of {sorted(valid)} (got {value!r}).")
                continue
            changes[field] = value

        if "due_date" in row and row["due_date"] is not None:
            raw_due = row["due_date"]
            if raw_due == "":
                changes["due_date"] = None
            else:
                try:
                    changes["due_date"] = date.fromisoformat(str(raw_due))
                except (TypeError, ValueError):
                    errors.append(
                        f"{where}: `due_date` must be an ISO 8601 date "
                        f"(got {raw_due!r}). Use '' to clear."
                    )

        # `start_date` ('' clears) and `tags` (names from the project's
        # catalogue, [] clears), checked the way `update_task` checks them (MIP-878).
        if "start_date" in row and row["start_date"] is not None:
            raw_start = row["start_date"]
            if raw_start == "":
                changes["start_date"] = None
            else:
                try:
                    changes["start_date"] = parse_iso_date(raw_start, "`start_date`")
                except ToolError as exc:
                    errors.append(f"{where}: {exc}")
        if "tags" in row and row["tags"] is not None:
            try:
                changes["tags"] = resolve_tags(row["tags"], task.project_id)
            except ToolError as exc:
                errors.append(f"{where}: {exc}")

        # Applied after the field diffs, through the milestone picker's own
        # bridge + cascade (see `move_task_to_milestone`); kept out of the
        # field loop because it writes hierarchy columns, not `field`.
        if "milestone_id" in row and row["milestone_id"] is not None:
            raw_milestone = row["milestone_id"]
            target: int | None = None
            valid = True
            if raw_milestone != "":
                try:
                    target = int(raw_milestone)
                except (TypeError, ValueError):
                    valid = False
                    errors.append(
                        f"{where}: `milestone_id` must be an integer "
                        f"(got {raw_milestone!r}). Use '' to detach."
                    )
            if valid:
                if error := milestone_move_error(task, target, ctx.team_id):
                    errors.append(f"{where}: {error}")
                else:
                    changes[_MILESTONE_KEY] = target

        # Assign / unassign, with the guard `assign_task` applies. A milestone's
        # backing row is refused: its people are `MilestoneAssignees`, and the
        # row's own `assignee_id` is only a mirror that `sync_backing_task`
        # would overwrite.
        if _ASSIGNEE_KEY in row and row[_ASSIGNEE_KEY] is not None:
            raw_assignee = row[_ASSIGNEE_KEY]
            if task.is_milestone:
                errors.append(
                    f"{where}: task {tid} is a milestone's backing task, whose people are "
                    "the milestone's assignees, not a single `assignee_id` — change them "
                    "on the milestone itself. Pass the milestone's tasks instead."
                )
            elif raw_assignee == "":
                changes[_ASSIGNEE_KEY] = None
            else:
                uid, why = _assignee_error(task, raw_assignee)
                if why:
                    errors.append(f"{where}: {why}")
                else:
                    changes[_ASSIGNEE_KEY] = uid

        rationale = (row.get("rationale") or "").strip()
        if not rationale:
            errors.append(f"{where}: `rationale` is required — say WHY in one sentence.")
        if not changes:
            errors.append(
                f"{where}: no changes given for task {tid} — provide at least one of "
                "priority / effort_level / status / due_date / start_date / tags / "
                "milestone_id / assignee_id."
            )
        planned.append((task, changes, rationale))

    if errors:
        raise ToolError(
            "Bulk update validation failed (nothing was applied):\n- " + "\n- ".join(errors[:25])
        )

    # Backing-task rows in the batch whose authoritative values live on a
    # MilestoneMaster side table. Fetched in one query so the apply loop
    # can mirror changes onto the milestone (and let `sync_backing_task`
    # push them back down) instead of writing the mirror row directly,
    # which the next milestone PATCH would silently revert — see
    # update_task's milestone branch for the full rationale.
    milestone_by_task_id = {
        m.task_id: m
        for m in MilestoneMaster.objects.filter(
            task_id__in=[t.task_id for t, _c, _r in planned if t.is_milestone],
            is_deleted=False,
        )
    }

    # ---- phase 2: apply all diffs atomically ----
    updated: list[dict[str, Any]] = []
    noops: list[int] = []
    # (task, who it was assigned to before) for every row whose assignee really changed:
    # the new assignees are told once the batch has committed (MIP-1461).
    reassigned: list[tuple[TaskMaster, Any]] = []
    try:
        with transaction.atomic():
            for task, changes, _rationale in planned:
                update_fields: list[str] = []
                changed: list[str] = []
                move = _MILESTONE_KEY in changes
                target_milestone_id = changes.pop(_MILESTONE_KEY, None)
                for field, new_value in changes.items():
                    # `assignee_id` reads back as a UUID and arrives as a
                    # string: compare as text, or every row looks changed and
                    # writes a history row and a calendar sync for nothing.
                    current = getattr(task, field)
                    if field == _ASSIGNEE_KEY:
                        differs = str(current or "") != str(new_value or "")
                        if differs:
                            reassigned.append((task, current))
                    elif field == "tags":
                        # Compared as name sets: the UI keeps no tag order, and
                        # the history signal ignores a reorder too. [] and NULL
                        # both mean no tags, stored as NULL like `update_task`.
                        differs = set(tag_names(current)) != set(tag_names(new_value))
                        new_value = new_value or None
                    else:
                        differs = current != new_value
                    if differs:
                        setattr(task, field, new_value)
                        update_fields.append(field)
                        changed.append(
                            f"{field}(cleared)"
                            if field in ("due_date", "start_date", "tags", _ASSIGNEE_KEY)
                            and not new_value
                            else field
                        )
                milestone = milestone_by_task_id.get(task.task_id)
                if not update_fields:
                    pass  # at most a milestone move — applied below
                elif milestone is not None:
                    # Mirror the changed columns onto the milestone (each
                    # shares its name with the backing task here) and let
                    # `sync_backing_task` re-derive the backing row so the
                    # milestone preview and the table stay in agreement.
                    for field in update_fields:
                        setattr(milestone, field, getattr(task, field))
                    milestone.save()
                    sync_backing_task(milestone)
                    task.refresh_from_db()
                else:
                    # "ts_updated_at" is required alongside the changed
                    # columns — auto_now only fires for fields named in
                    # update_fields, and the incremental reindexer keys off
                    # it. See update_task. (`sync_backing_task` above saves
                    # the backing row wholesale, bumping every auto_now
                    # field, so the milestone branch needs no explicit list.)
                    task.save(update_fields=[*update_fields, "ts_updated_at"])
                if move and move_task_to_milestone(task, target_milestone_id):
                    changed.append(
                        "milestone" if target_milestone_id is not None else "milestone(cleared)"
                    )
                if not changed:
                    noops.append(task.task_id)
                    continue
                updated.append(
                    {
                        "task_id": task.task_id,
                        "display_id": task.display_id,
                        "title": task.title,
                        "project_id": task.project_id,
                        "changed_fields": changed,
                    }
                )
    except Exception as e:  # noqa: BLE001 — surface as ToolError for the model
        # The model and the MCP caller read this; the database error
        # goes to the log with a reference (MIP-622).
        ref = log_failure("update_tasks_bulk tool failed", e)
        raise ToolError(f"Bulk update failed (nothing was applied) (ref {ref}).")

    # Whoever was handed a task is told, as in the app (MIP-1461). After the commit, so a
    # rolled-back batch tells nobody.
    for task, previous_assignee_id in reassigned:
        notify_task_assigned(task, actor_id=ctx.user_id, previous_assignee_id=previous_assignee_id)

    # Priorities/statuses feed the project table's ordering — drop each
    # touched project's cached listing after commit.
    for pid in {row["project_id"] for row in updated}:
        if pid is not None:
            invalidate_project_tasks_cache(ctx.team_id, pid)

    field_counts: dict[str, int] = {}
    for row in updated:
        for f in row["changed_fields"]:
            key = f.removesuffix("(cleared)")
            field_counts[key] = field_counts.get(key, 0) + 1
    parts = [f"{n} {field}" for field, n in sorted(field_counts.items())] or ["no-op"]
    summary = f"Updated {len(updated)} task(s): " + ", ".join(parts)
    if noops:
        summary += f" ({len(noops)} already up to date)"

    return {
        "updated": updated,
        "noops": noops,
        "__summary__": summary,
    }


UPDATE_TASKS_BULK = Tool(
    name="update_tasks_bulk",
    description=(
        "Change priority / due_date / start_date / status / effort_level / tags / "
        "milestone_id / assignee_id on MANY tasks in ONE approval — the tool for 'reprioritize', 'organize', "
        "or 'clean up' the tasks in a milestone or project, or for filing a "
        "batch of tasks under a milestone. REQUIRES USER APPROVAL — "
        "the user sees every proposed change WITH your per-task rationale "
        "and approves once. ALWAYS read current state first (list_tasks, "
        "and list_task_dependencies for blocker ordering) so every change "
        "is a real, justified change. NEVER call update_task repeatedly "
        "for a batch; update_task remains correct for ONE task. All-or-"
        "nothing: one invalid entry rejects the whole batch. Cannot set "
        "status to Deleted."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "updates": {
                "type": "ARRAY",
                "description": (
                    f"1-{_MAX_BULK_UPDATES} per-task changes. Include ONLY "
                    "tasks that actually change, and only the fields that "
                    "change."
                ),
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "task_id": {
                            "type": "INTEGER",
                            "description": "Numeric task id (from list_tasks).",
                        },
                        "priority": {
                            "type": "STRING",
                            "enum": PRIORITY_ENUM,
                            "description": "New priority. Omit to leave unchanged.",
                        },
                        "due_date": {
                            "type": "STRING",
                            "description": (
                                "New ISO 8601 date (YYYY-MM-DD). Omit to leave "
                                "unchanged. Pass '' to clear."
                            ),
                        },
                        "start_date": {
                            "type": "STRING",
                            "description": (
                                "New planned start, ISO 8601 date (YYYY-MM-DD). Omit "
                                "to leave unchanged. Pass '' to clear."
                            ),
                        },
                        "tags": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                            "description": (
                                "The task's full new set of tag names, from "
                                "list_project_tags (the task's project). Replaces the "
                                "current tags: include any you are keeping. Pass [] "
                                "to clear. An unknown name rejects the whole batch. "
                                "Omit to leave unchanged."
                            ),
                        },
                        "status": {
                            "type": "STRING",
                            "enum": STATUS_ENUM,
                            "description": "New status. Omit to leave unchanged.",
                        },
                        "effort_level": {
                            "type": "STRING",
                            "enum": EFFORT_ENUM,
                            "description": "New effort estimate. Omit to leave unchanged.",
                        },
                        "milestone_id": {
                            "type": "STRING",
                            "description": (
                                "Milestone id (from list_milestones, same project) "
                                "to move the task into; its sub-tasks move with "
                                "it. Omit to leave unchanged. Pass '' to take the "
                                "task out of its milestone."
                            ),
                        },
                        "assignee_id": {
                            "type": "STRING",
                            "description": (
                                "User UUID to assign (get_team_members / "
                                "list_project_members): a teammate or someone on "
                                "the task's project. Omit to leave unchanged. "
                                "Pass '' to unassign. Not for a milestone's own "
                                "backing task."
                            ),
                        },
                        "rationale": {
                            "type": "STRING",
                            "description": (
                                "One sentence: WHY this change (the user reads "
                                "this in the approval card)."
                            ),
                        },
                    },
                    "required": ["task_id", "rationale"],
                },
            },
        },
        "required": ["updates"],
    },
    run=_run,
    requires_approval=True,
)
