"""`manage_milestone` write tool — milestones end to end (MIP-879).

Before this a milestone could only be created inside `create_task_plan`
(which needs at least one task), and nothing could change its assignees, file
it in a folder, link a sprint, set its start date, or manage the folders. One
tool, with an `action`, so the in-app prompt pays for one declaration:

  create         a milestone with no tasks yet
  update         any of its fields, assignees, folder and sprint
  create_folder / rename_folder / delete_folder    the project's milestone folders
  convert        a root task into a milestone, or a milestone back into a task   (flag, MIP-1564)

`update` also takes `project_id` while `AGENT_PARITY_TOOLS` is on: the REST PATCH's project move
(`update_milestone(project_move=...)`). Both are `convert` / `project_id` below.

Every write goes through the same service the UI's endpoints call
(`services/milestone_manage.py`, `services/milestone_service.py`), so the
milestone, its backing task row, its assignees and its tasks' sprint end up
exactly where a person's edit leaves them. Everything is validated before
anything is written: a refused call changes nothing, even when it also carried
valid changes. Deleting a milestone is its own tool (`delete_milestone`), so
the MCP opt-in for destructive tools can cover it alone.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from django.db import transaction

from apis.request_id import log_failure
from origin.models.task.milestone_models import (
    MilestoneAssignees,
    MilestoneFolder,
    MilestoneMaster,
)
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.chip_syntax import CHIP_SYNTAX_HINT
from origin.search_engine.agent.parity import parity_on
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.blocknote_md import task_body_blocks
from origin.search_engine.agent.tools.body_limits import check_body_size
from origin.search_engine.agent.tools.body_mentions import notify_task_body_mentions
from origin.search_engine.agent.tools.create_task_plan import _assignee_errors
from origin.search_engine.agent.tools.entity_chips import chip_resolver
from origin.search_engine.agent.tools.list_milestones import _backing_display_id
from origin.search_engine.agent.tools.milestone_access import (
    load_folder,
    load_milestone,
    load_project,
)
from origin.search_engine.agent.tools.milestone_convert import (
    converted_hint,
    refusal_sentence,
    whole_number,
)
from origin.search_engine.agent.tools.task_access import load_task
from origin.search_engine.agent.tools.task_enums import (
    EFFORT_ENUM,
    PRIORITY_ENUM,
    STATUS_ENUM,
    VALID_EFFORTS,
    VALID_PRIORITIES,
    VALID_STATUSES,
)
from origin.search_engine.agent.tools.task_tags import resolve_tags, tag_names
from origin.search_engine.quota import TASK_CREATE_KEY, check_remaining_monthly, increment_usage
from origin.services.collab_push import push_task_body
from origin.services.milestone_manage import (
    FOLDER_NAME_MAX,
    UNSET,
    clean_folder_name,
    create_folder,
    folder_name_taken,
    lock_live_milestone,
    move_assignee_problem,
    update_milestone,
)
from origin.services.milestone_service import create_milestone
from origin.services.task_assignment_notice import notify_milestone_assigned
from origin.services.task_cache import invalidate_project_tasks_cache
from origin.services.task_card_post import post_milestone_created_card
from origin.services.task_milestone_conversion import (
    ConversionRefused,
    convert_milestone_to_task,
    convert_task_to_milestone,
)
from origin.services.task_project_move import milestone_subtree_task_ids
from origin.views.utils.scope_guards import can_access_task, is_project_member

_TITLE_MAX = MilestoneMaster._meta.get_field("title").max_length

ACTIONS = ("create", "update", "create_folder", "rename_folder", "delete_folder")
# While `AGENT_PARITY_TOOLS` is on (MIP-1564): the same, and `convert`. Appended, so the order of
# the others never moves.
ACTIONS_ON = (*ACTIONS, "convert")

# The arguments that describe the milestone itself (create and update).
_MILESTONE_FIELDS = frozenset(
    {
        "title",
        "description_markdown",
        "start_date",
        "due_date",
        "priority",
        "effort_level",
        "status",
        "tags",
        "assignee_ids",
        "milestone_folder_id",
        "sprint_id",
    }
)
# What each action takes. Anything else that is set is refused, so a field
# that would be ignored (a `title` on `delete_folder`) can't read as success.
_ALLOWED = {
    "create": {"project_id"} | _MILESTONE_FIELDS,
    "update": {"milestone_id"} | _MILESTONE_FIELDS,
    "create_folder": {"project_id", "folder_name"},
    "rename_folder": {"milestone_folder_id", "folder_name"},
    "delete_folder": {"milestone_folder_id"},
}
# What the flag adds to what an action takes: `convert`'s two ids, and the destination of a move.
_ALLOWED_ON = {
    "update": {"project_id"},
    "convert": {"task_id", "milestone_id"},
}
_REQUIRED = {
    "create": ("project_id", "title"),
    "update": ("milestone_id",),
    "create_folder": ("project_id", "folder_name"),
    "rename_folder": ("milestone_folder_id", "folder_name"),
    "delete_folder": ("milestone_folder_id",),
}


def _allowed(action: str) -> set[str]:
    """The arguments `action` takes right now: with the flag on, a few more."""
    allowed = set(_ALLOWED.get(action, ()))
    if parity_on():
        allowed |= _ALLOWED_ON.get(action, set())
    return allowed


def _check_arguments(action: str, args: dict[str, Any]) -> None:
    """The right arguments for the action, before anything is looked up."""
    if action == "update" and args.get("project_id") is not None and not parity_on():
        raise ToolError(
            "A milestone can't be moved to another project with this tool, and "
            "`project_id` isn't an argument of `update`. Nothing was changed."
        )
    allowed = _allowed(action)
    unexpected = sorted(
        key
        for key, value in args.items()
        if key != "action" and value is not None and key not in allowed
    )
    if unexpected:
        raise ToolError(
            f"{', '.join(f'`{k}`' for k in unexpected)} does not apply to `{action}`; it takes "
            f"{', '.join(f'`{k}`' for k in sorted(allowed))}. Nothing was changed."
        )
    missing = [key for key in _REQUIRED.get(action, ()) if args.get(key) in (None, "")]
    if missing:
        raise ToolError(
            f"`{action}` needs {', '.join(f'`{k}`' for k in missing)}. Nothing was changed."
        )


def _parse_fields(
    args: dict[str, Any], project, ctx: ToolContext, *, creating: bool
) -> tuple[dict[str, Any], list[str]]:
    """The milestone arguments that were sent, validated and turned into what
    the service takes, plus every problem found (so one refusal names them all).
    Only keys that were sent appear in the result."""
    out: dict[str, Any] = {}
    errors: list[str] = []

    if args.get("title") is not None:
        title = args["title"].strip() if isinstance(args["title"], str) else ""
        if not title:
            errors.append("`title` must be non-empty.")
        elif len(title) > _TITLE_MAX:
            errors.append(
                f"`title` is too long ({len(title)} characters; the limit is {_TITLE_MAX})."
            )
        else:
            out["title"] = title

    if args.get("description_markdown") is not None:
        if not isinstance(args["description_markdown"], str):
            errors.append("`description_markdown` must be a markdown string.")
        else:
            out["description"] = task_body_blocks(
                args["description_markdown"],
                entity_chip_resolver=chip_resolver(team_id=ctx.team_id, user_id=ctx.user_id),
            )

    for key, valid in (("priority", VALID_PRIORITIES), ("effort_level", VALID_EFFORTS)):
        raw = args.get(key)
        if raw is None or (raw == "" and creating):
            continue
        if raw not in valid:
            errors.append(f"`{key}` must be one of {sorted(valid)} (got {raw!r}).")
        else:
            out[key] = raw

    raw_status = args.get("status")
    if raw_status is not None and not (raw_status == "" and creating):
        if raw_status == "Deleted":
            errors.append(
                "`status` can't be 'Deleted' here: that is only half of deleting a milestone. "
                "Use `delete_milestone`."
            )
        elif raw_status not in VALID_STATUSES:
            errors.append(f"`status` must be one of {sorted(VALID_STATUSES)} (got {raw_status!r}).")
        else:
            out["status"] = raw_status

    for key in ("start_date", "due_date"):
        raw = args.get(key)
        if raw is None:
            continue
        if raw == "":
            out[key] = None
            continue
        try:
            out[key] = date.fromisoformat(str(raw))
        except ValueError:
            errors.append(f"`{key}` must be an ISO 8601 date such as 2026-06-30 (got {raw!r}).")

    if args.get("tags") is not None:
        try:
            out["tags"] = resolve_tags(args["tags"], project.project_id)
        except ToolError as e:
            errors.append(str(e))

    raw_assignees = args.get("assignee_ids")
    if raw_assignees is not None:
        if raw_assignees == "":
            raw_assignees = []
        if not isinstance(raw_assignees, list) or not all(
            isinstance(u, str) for u in raw_assignees
        ):
            errors.append("`assignee_ids` must be a list of user ids ([] clears them all).")
        else:
            wanted: list[str] = []
            for uid in raw_assignees:
                uid = uid.strip()
                if uid and uid not in wanted:
                    wanted.append(uid)
            problems = (
                _assignee_errors(
                    set(wanted), task_acl_user_ids(project.project_id, None, None), ctx
                )
                if wanted
                else []
            )
            errors.extend(problems)
            if not problems:
                out["assignee_ids"] = [str(uuid.UUID(uid)) for uid in wanted]

    if args.get("milestone_folder_id") is not None:
        raw = args["milestone_folder_id"]
        if raw == "":
            out["folder"] = None
        else:
            try:
                folder_id = int(raw)
            except (TypeError, ValueError):
                errors.append(
                    f"`milestone_folder_id` must be a folder id (got {raw!r}). '' takes the "
                    "milestone out of its folder."
                )
            else:
                folder = MilestoneFolder.objects.filter(
                    folder_id=folder_id, project_id=project.project_id
                ).first()
                if folder is None:
                    errors.append(
                        f"Milestone folder {folder_id} was not found in this project. "
                        "`list_milestones` returns the project's folders."
                    )
                else:
                    out["folder"] = folder

    if args.get("sprint_id") is not None:
        raw = args["sprint_id"]
        if raw == "":
            out["sprint"] = None
        else:
            try:
                sprint_id = int(raw)
            except (TypeError, ValueError):
                errors.append(
                    f"`sprint_id` must be a sprint id (got {raw!r}). '' takes the milestone "
                    "out of its sprint."
                )
            else:
                sprint = Sprint.objects.filter(
                    sprint_id=sprint_id, project_id=project.project_id, is_deleted=False
                ).first()
                if sprint is None:
                    errors.append(f"Sprint {sprint_id} was not found in this project.")
                else:
                    out["sprint"] = sprint

    return out, errors


def _refuse(errors: list[str], nothing: str) -> ToolError:
    return ToolError(f"{nothing}:\n- " + "\n- ".join(errors[:25]))


def _milestone_payload(milestone_id: int) -> dict[str, Any]:
    """The milestone as stored, re-read after the write."""
    m = MilestoneMaster.objects.select_related("project", "task").get(milestone_id=milestone_id)
    assignee_ids = [
        str(uid)
        for uid in MilestoneAssignees.objects.filter(milestone=m)
        .order_by("ts_created_at", "id")
        .values_list("user_id", flat=True)
    ]
    return {
        "milestone_id": m.milestone_id,
        "task_id": m.task_id,
        "display_id": _backing_display_id(m),
        "project_id": m.project_id,
        "project_name": m.project.project_name,
        "title": m.title,
        "status": m.status,
        "priority": m.priority or None,
        "effort_level": m.effort_level or None,
        "start_date": m.start_date.isoformat() if m.start_date else None,
        "due_date": m.due_date.isoformat() if m.due_date else None,
        "tags": tag_names(m.tags),
        "assignee_ids": assignee_ids,
        "folder_id": m.folder_id,
        "sprint_id": m.sprint_id,
    }


def _label(payload: dict[str, Any]) -> str:
    return payload["display_id"] or f"milestone {payload['milestone_id']}"


# --------------------------------------------------------------------------
# create / update
# --------------------------------------------------------------------------


def _create(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    project = load_project(args["project_id"], ctx)
    parsed, errors = _parse_fields(args, project, ctx, creating=True)
    if errors:
        raise _refuse(errors, "Nothing was created")

    # The milestone's backing task counts against the monthly task cap, as it
    # does inside `create_task_plan`. Checked up front, charged after the write.
    within_cap, _used, task_limit = check_remaining_monthly(ctx.user_id, TASK_CREATE_KEY)
    if not within_cap:
        raise ToolError(
            f"You've created all {task_limit} tasks for this month, and a milestone counts as "
            "one. Upgrade your plan to keep going."
        )

    try:
        with transaction.atomic():
            milestone = create_milestone(
                project,
                # A UUID, as the UI's POST passes (`request.user.id`): a string
                # in memory is compared with the stored UUID on the backing
                # task's next save and read as a change of reporter and
                # assignee, leaving two spurious history rows each.
                reporter_id=uuid.UUID(str(ctx.user_id)),
                title=parsed["title"],
                description_blocks=parsed.get("description"),
                status=parsed.get("status") or "Open",
                priority=parsed.get("priority"),
                effort_level=parsed.get("effort_level"),
                start_date=parsed.get("start_date"),
                due_date=parsed.get("due_date"),
                sprint=parsed.get("sprint"),
                folder=parsed.get("folder"),
                tags=parsed.get("tags") or None,
                assignee_ids=parsed.get("assignee_ids") or [],
            )
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_milestone create failed", e)
        raise ToolError(f"Failed to create the milestone (nothing was created) (ref {ref}).")

    # The backing task is a new row in the project task table.
    invalidate_project_tasks_cache(milestone.team_id, milestone.project_id)
    increment_usage(ctx.user_id, TASK_CREATE_KEY)
    if parsed.get("description") and milestone.task_id:
        backing = TaskMaster.objects.select_related("project").get(task_id=milestone.task_id)
        notify_task_body_mentions(backing, actor_id=ctx.user_id)
    # The project chat's "milestone created" card, which the app's browser would post
    # (MIP-1450): it is what tells the assignees.
    post_milestone_created_card(milestone, actor_id=ctx.user_id)

    payload = _milestone_payload(milestone.milestone_id)
    return {
        "action": "create",
        **payload,
        "__summary__": f'Created milestone {_label(payload)} "{payload["title"]}"',
    }


# What a project move clears, and so what a call that MOVES may not also set: they belong to the project
# being left (a sprint, a folder) or are its labels (tags).
_CLEARED_BY_A_MOVE = ("sprint_id", "milestone_folder_id", "tags")


def _destination(args: dict[str, Any], milestone: MilestoneMaster, ctx: ToolContext):
    """The project an `update` moves the milestone to, or None when it does not move.

    Only while the flag is on (`_check_arguments` refuses a `project_id` on update otherwise). The
    destination is judged as the app's PATCH judges it (a live project the caller is a member of)
    and, as `update_task` does, it has to be in the caller's team: a project of another team, which
    the app's PATCH would accept for a member of both, is answered "not found" here. The project the
    milestone is already in is not a move, as in the PATCH.
    """
    raw = args.get("project_id")
    if not parity_on() or raw in (None, ""):
        return None
    destination = load_project(whole_number(raw, "project_id"), ctx)
    if destination.project_id == milestone.project_id:
        return None
    given = [key for key in _CLEARED_BY_A_MOVE if args.get(key) is not None]
    if given:
        raise ToolError(
            f"{', '.join(f'`{k}`' for k in given)} can't be set in the same call as a project "
            "move: a move clears the milestone's sprint, folder and tags, because they belong to "
            "the project it leaves. Move it first, then set them with a second `update` in "
            f"{destination.project_name}. Nothing was changed."
        )
    return destination


def _update(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    milestone = load_milestone(args["milestone_id"], ctx)
    destination = _destination(args, milestone, ctx)
    # The people are judged against the project the milestone ends up in, as the PATCH judges them.
    parsed, errors = _parse_fields(args, destination or milestone.project, ctx, creating=False)
    if errors:
        raise _refuse(errors, "Nothing was changed")
    # The people already on the milestone and its tasks are judged against the destination too, as
    # the PATCH judges them (MIP-1583): a task must not stay open to an assignee who is not on the
    # project it now lives in.
    if destination is not None and (
        problem := move_assignee_problem(
            milestone, destination, assignee_ids=parsed.get("assignee_ids", UNSET)
        )
    ):
        raise ToolError(problem)

    fields: dict[str, Any] = {}
    changed: list[str] = []
    for key in ("title", "status", "priority", "effort_level"):
        if key in parsed and parsed[key] != getattr(milestone, key):
            fields[key] = parsed[key]
            changed.append(key)
    # A fresh body always differs (every block gets a new id), so a description
    # that was sent is written unless both it and the stored one are empty.
    if "description" in parsed and (parsed["description"] or milestone.description):
        fields["description"] = parsed["description"]
        changed.append("description")
    # Compared as name sets: the UI keeps no tag order.
    if "tags" in parsed and set(tag_names(parsed["tags"])) != set(tag_names(milestone.tags)):
        fields["tags"] = parsed["tags"] or None
        changed.append("tags" if parsed["tags"] else "tags(cleared)")

    dates: dict[str, Any] = {}
    for key in ("start_date", "due_date"):
        if key in parsed and parsed[key] != getattr(milestone, key):
            dates[key] = parsed[key]
            changed.append(key if parsed[key] is not None else f"{key}(cleared)")

    sprint = UNSET
    if "sprint" in parsed and (parsed["sprint"].sprint_id if parsed["sprint"] else None) != (
        milestone.sprint_id
    ):
        sprint = parsed["sprint"]
        changed.append("sprint" if sprint is not None else "sprint(cleared)")
    folder = UNSET
    if "folder" in parsed and (parsed["folder"].folder_id if parsed["folder"] else None) != (
        milestone.folder_id
    ):
        folder = parsed["folder"]
        changed.append("folder" if folder is not None else "folder(cleared)")
    assignee_ids = UNSET
    if "assignee_ids" in parsed:
        current = {
            str(uid)
            for uid in MilestoneAssignees.objects.filter(milestone=milestone).values_list(
                "user_id", flat=True
            )
        }
        if set(parsed["assignee_ids"]) != current:
            assignee_ids = parsed["assignee_ids"]
            changed.append("assignees" if assignee_ids else "assignees(cleared)")

    # Read before the write: the milestone object is moved in memory by it.
    source = (milestone.team_id, milestone.project_id, milestone.project.project_name)
    moved_tasks = 0
    if destination is not None:
        changed.insert(0, "project")
        # The tasks that go with it that a person still sees (a deleted one moves too, silently):
        # not the milestone's own row, which is the milestone.
        moved_tasks = TaskMaster.objects.filter(
            task_id__in=milestone_subtree_task_ids(milestone) - {milestone.task_id},
            is_deleted=False,
        ).count()

    if not changed:
        payload = _milestone_payload(milestone.milestone_id)
        return {
            "action": "update",
            **payload,
            "changed_fields": [],
            "__summary__": f"No changes applied to {_label(payload)}.",
        }

    added_assignees: list[str] = []
    report: dict[str, Any] = {}
    try:
        with transaction.atomic():
            if lock_live_milestone(milestone, project_id=source[1]) is None:
                # Locked until this commits, so a convert or a second move waits: the full save
                # below would otherwise write the stale copy of the row back over theirs (a move
                # since MIP-1564, every update since MIP-1583, as the app's PATCH does).
                raise ToolError(
                    f"Milestone {milestone.milestone_id} was changed while this was being applied "
                    "(converted, deleted or moved by someone else). Nothing was changed."
                )
            added_assignees = update_milestone(
                milestone,
                fields=fields,
                start_date=dates.get("start_date", UNSET),
                due_date=dates.get("due_date", UNSET),
                sprint=sprint,
                folder=folder,
                assignee_ids=assignee_ids,
                project_move=destination,
                report=report,
            )
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_milestone update failed", e)
        raise ToolError(f"Failed to update the milestone (ref {ref}).")

    # The backing task mirrors most of these fields and a sprint change reaches
    # every task in the milestone.
    invalidate_project_tasks_cache(milestone.team_id, milestone.project_id)
    if destination is not None:
        # A move also emptied the project it left, keyed by the team that owned it.
        invalidate_project_tasks_cache(source[0], source[1])
    # Whoever was just added to the milestone is told, as in the app (MIP-1464).
    notify_milestone_assigned(milestone, actor_id=ctx.user_id, added_user_ids=added_assignees)

    if "description" in fields and milestone.task_id:
        # The editor renders the task's Yjs doc, not `content`; without this push
        # a milestone that was ever opened kept its old body on screen, and the
        # next keystroke saved the old body back (MIP-694). Then the `@` chips
        # ping their people, as a typed one does (MIP-708).
        backing = TaskMaster.objects.select_related("project").get(task_id=milestone.task_id)
        push_task_body(backing.task_id, backing.content or [])
        notify_task_body_mentions(backing, actor_id=ctx.user_id)

    payload = _milestone_payload(milestone.milestone_id)
    result: dict[str, Any] = {
        "action": "update",
        **payload,
        "changed_fields": changed,
        "__summary__": f"Updated {_label(payload)}: {', '.join(changed)}",
    }
    if destination is not None:
        # What the move did, for the model to report and not to guess: the display ids are new, and
        # what belonged to the old project is gone.
        result["moved"] = {
            "from_project_id": source[1],
            "from_project_name": source[2],
            "tasks_moved": moved_tasks,
            "cleared": ["sprint", "folder", "tags"],
            "display_ids_changed": True,
        }
        # Task notes whose author is not on the destination lost their Owner role with the move
        # (recorded: a move to a project they are on writes it back, MIP-1583). Said only when it
        # happened, so the result of a move that narrowed nothing is what it always was.
        narrowed = report.get("notes_narrowed", 0)
        if narrowed:
            result["moved"]["notes_narrowed"] = narrowed
        result["__summary__"] = (
            f"Moved {_label(payload)} from {source[2]} to {destination.project_name} with "
            f"{moved_tasks} task(s); new display ids, sprint, folder and tags cleared"
            + (f"; also changed: {', '.join(changed[1:])}" if len(changed) > 1 else "")
            + (
                f"; {narrowed} task note(s) lost their author's Owner role, as the author is not "
                f"on {destination.project_name}"
                if narrowed
                else ""
            )
        )
    return result


# --------------------------------------------------------------------------
# convert (MIP-1564, behind AGENT_PARITY_TOOLS)
# --------------------------------------------------------------------------


def _convert(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Turn a root task into a milestone, or a milestone into a task, as the app's two endpoints do.
    The caller names the item by the argument that fits it: `task_id` for the task to promote,
    `milestone_id` for the milestone to demote; the direction follows."""
    task_ref, milestone_ref = args.get("task_id"), args.get("milestone_id")
    has_task, has_milestone = task_ref not in (None, ""), milestone_ref not in (None, "")
    if has_task == has_milestone:
        raise ToolError(
            "`convert` needs exactly one of `task_id` (a root task to turn into a milestone) or "
            "`milestone_id` (a milestone to turn into a task). Nothing was changed."
        )
    if has_task:
        return _task_to_milestone(whole_number(task_ref, "task_id"), ctx)
    return _milestone_to_task(whole_number(milestone_ref, "milestone_id"), ctx)


def _refused(refusal: ConversionRefused, *, milestone_id: int | None = None) -> ToolError:
    return ToolError(refusal_sentence(refusal.code, refusal.message, milestone_id=milestone_id))


def _task_to_milestone(raw_task_id: Any, ctx: ToolContext) -> dict[str, Any]:
    # Who may open the task (the app's `can_access_task`, plus the run's team), then who belongs to
    # its project: an assignee who was never added can open it but cannot restructure the project's
    # milestones (the same bar as creating or deleting one).
    task = load_task(raw_task_id, ctx, action="convert")
    if not is_project_member(task.project_id, ctx.user_id):
        raise ToolForbidden(
            f"Only a member of {task.display_id}'s project can turn it into a milestone, and you "
            "are not on that project. Nothing was changed."
        )
    try:
        milestone = convert_task_to_milestone(task.task_id)
    except ConversionRefused as refusal:
        raise _refused(refusal, milestone_id=task.milestone_id) from None
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_milestone convert failed", e)
        raise ToolError(f"Failed to convert the task (nothing was changed) (ref {ref}).")

    payload = _milestone_payload(milestone.milestone_id)
    in_it = TaskMaster.objects.filter(milestone=milestone).exclude(task_id=task.task_id).count()
    return {
        "action": "convert",
        "direction": "task_to_milestone",
        **payload,
        "tasks_in_milestone": in_it,
        "__summary__": (
            f'Converted {_label(payload)} "{payload["title"]}" into a milestone '
            f"(milestone {payload['milestone_id']}); {in_it} sub-task(s) are now in it"
        ),
    }


def _milestone_to_task(raw_milestone_id: Any, ctx: ToolContext) -> dict[str, Any]:
    milestone = load_milestone(raw_milestone_id, ctx, allow_deleted=True)
    if milestone.is_deleted:
        raise ToolError(_already_gone(milestone, ctx))
    try:
        row, retired, kept = convert_milestone_to_task(milestone.milestone_id)
    except ConversionRefused as refusal:
        raise _refused(refusal) from None
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_milestone convert failed", e)
        raise ToolError(f"Failed to convert the milestone (nothing was changed) (ref {ref}).")

    collaborators = sorted(str(uid) for uid in row.collaborators.values_list("id", flat=True))
    # No `milestone_id` key: there is no milestone any more, and a result that carried one would be
    # read as a live milestone (the chip a write leaves, the use marker).
    return {
        "action": "convert",
        "direction": "milestone_to_task",
        "task_id": row.task_id,
        "display_id": row.display_id,
        "title": row.title,
        "project_id": row.project_id,
        "project_name": row.project.project_name,
        "converted_milestone_id": retired.milestone_id,
        "sub_tasks_kept": kept,
        "assignee_id": str(row.assignee_id) if row.assignee_id else None,
        "collaborator_ids": collaborators,
        "__summary__": (
            f'Converted milestone {retired.milestone_id} "{row.title}" into the task '
            f"{row.display_id}; {kept} task(s) stay under it as sub-tasks, its folder and sprint "
            "were dropped"
        ),
    }


def _already_gone(milestone: MilestoneMaster, ctx: ToolContext) -> str:
    """Why a retired milestone can't be converted: it was turned into a task (say which, when the
    caller may open it, as the app's 410 does) or it was deleted."""
    gone = f"Milestone {milestone.milestone_id} "
    task = (
        TaskMaster.objects.filter(task_id=milestone.converted_to_task_id).first()
        if milestone.converted_to_task_id
        else None
    )
    if task is None or task.is_deleted or not can_access_task(task.task_id, ctx.user_id):
        return gone + "has been deleted."
    return gone + converted_hint(
        task.display_id, task.task_id, task.milestone_id if task.is_milestone else None
    )


# --------------------------------------------------------------------------
# folders
# --------------------------------------------------------------------------


def _folder_name(raw: Any) -> str:
    name = clean_folder_name(raw)
    if name is None:
        raise ToolError(
            f"`folder_name` is required, at most {FOLDER_NAME_MAX} characters. Nothing was changed."
        )
    return name


def _folder_payload(folder: MilestoneFolder) -> dict[str, Any]:
    return {
        "folder_id": folder.folder_id,
        "project_id": folder.project_id,
        "project_name": folder.project.project_name,
        "name": folder.name,
        "sort_order": folder.sort_order,
    }


def _create_folder(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    project = load_project(args["project_id"], ctx)
    name = _folder_name(args["folder_name"])
    if folder_name_taken(project.project_id, name):
        raise ToolError(
            f'Project "{project.project_name}" already has a folder named "{name}". '
            "Nothing was created."
        )
    try:
        folder = create_folder(project, name)
    except Exception as e:  # noqa: BLE001 — e.g. a second create racing this one
        ref = log_failure("manage_milestone create_folder failed", e)
        raise ToolError(f"Failed to create the folder (nothing was created) (ref {ref}).")
    return {
        "action": "create_folder",
        **_folder_payload(folder),
        "__summary__": f'Created milestone folder "{name}" in {project.project_name}',
    }


def _rename_folder(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    folder = load_folder(args["milestone_folder_id"], ctx)
    name = _folder_name(args["folder_name"])
    if name == folder.name:
        return {
            "action": "rename_folder",
            **_folder_payload(folder),
            "changed_fields": [],
            "__summary__": f'Milestone folder "{name}" already has that name.',
        }
    if folder_name_taken(folder.project_id, name, exclude_folder_id=folder.folder_id):
        raise ToolError(f'This project already has a folder named "{name}". Nothing was changed.')
    before = folder.name
    folder.name = name
    try:
        folder.save()
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_milestone rename_folder failed", e)
        raise ToolError(f"Failed to rename the folder (ref {ref}).")
    return {
        "action": "rename_folder",
        **_folder_payload(folder),
        "changed_fields": ["name"],
        "__summary__": f'Renamed milestone folder "{before}" to "{name}"',
    }


def _delete_folder(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    folder = load_folder(args["milestone_folder_id"], ctx)
    payload = _folder_payload(folder)
    # Live milestones that sit in it. They are not deleted: the foreign key
    # clears, so they are filed nowhere, as the app's confirm text says.
    filed = MilestoneMaster.objects.filter(folder=folder, is_deleted=False).count()
    try:
        folder.delete()
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_milestone delete_folder failed", e)
        raise ToolError(f"Failed to delete the folder (ref {ref}).")
    return {
        "action": "delete_folder",
        **payload,
        "deleted": True,
        "milestones_unfiled": filed,
        "__summary__": (
            f'Deleted milestone folder "{payload["name"]}"'
            + (f"; {filed} milestone(s) in it are now unfiled" if filed else "")
        ),
    }


_HANDLERS = {
    "create": _create,
    "update": _update,
    "create_folder": _create_folder,
    "rename_folder": _rename_folder,
    "delete_folder": _delete_folder,
}
_HANDLERS_ON = {**_HANDLERS, "convert": _convert}


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    action = args.get("action")
    # With the flag off `convert` is no action at all, refused in the words every unknown one is.
    on = parity_on()
    if action not in (_HANDLERS_ON if on else _HANDLERS):
        raise ToolError(
            f"`action` must be one of {list(ACTIONS_ON if on else ACTIONS)} (got {action!r})."
        )
    _check_arguments(action, args)
    check_body_size(args.get("description_markdown"), "description_markdown")
    return (_HANDLERS_ON if on else _HANDLERS)[action](args, ctx)


MANAGE_MILESTONE = Tool(
    name="manage_milestone",
    description=(
        "Create or change a MILESTONE, or manage a project's milestone FOLDERS. REQUIRES "
        "USER APPROVAL. One tool; `action` says which: 'create' (a milestone with no tasks "
        "yet; needs project_id and title; for a milestone WITH its tasks use "
        "create_task_plan), 'update' (needs milestone_id; send only the fields to change), "
        "'create_folder' (project_id + folder_name), 'rename_folder' (milestone_folder_id + "
        "folder_name), 'delete_folder' (milestone_folder_id; the milestones in it are NOT "
        "deleted, they become unfiled). For create and update: title, description_markdown "
        "(replaces the whole description), start_date, due_date, priority, effort_level, "
        "status, tags (names from list_project_tags; replaces), assignee_ids (the milestone's "
        "FULL set of assignees; replaces), milestone_folder_id and sprint_id ('' takes the "
        "milestone out of its folder / sprint). Look the ids up first: project_id "
        "(list_projects), milestone_id and folders (list_milestones), sprint_id (list_sprints), "
        "people (get_team_members / list_project_members). Moving a milestone to another "
        "project is not supported. Deleting a milestone is delete_milestone, not a status. "
        "A call with any invalid argument changes nothing."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": list(ACTIONS),
                "description": "What to do.",
            },
            "project_id": {
                "type": "INTEGER",
                "description": "Project (from list_projects). For create and create_folder.",
            },
            "milestone_id": {
                "type": "INTEGER",
                "description": "Milestone to change (from list_milestones). For update.",
            },
            "title": {
                "type": "STRING",
                "description": f"Milestone title (1 line, at most {_TITLE_MAX} characters).",
            },
            "description_markdown": {
                "type": "STRING",
                "description": (
                    "The milestone's description in markdown, replacing the current one. "
                    "Follow the house milestone template: '### 🎯 Goal', '### ✅ Success "
                    "criteria' (bulleted), '### 📦 In scope', '### 🚫 Out of scope', "
                    "'### ⚠️ Risks & dependencies', and '### 🧭 Order of work' once it has "
                    "tasks. Pass '' to clear it." + CHIP_SYNTAX_HINT
                ),
            },
            "start_date": {
                "type": "STRING",
                "description": "ISO date (YYYY-MM-DD). '' clears it.",
            },
            "due_date": {
                "type": "STRING",
                "description": "ISO date (YYYY-MM-DD). '' clears it.",
            },
            "priority": {"type": "STRING", "enum": PRIORITY_ENUM, "description": "Priority."},
            "effort_level": {
                "type": "STRING",
                "enum": EFFORT_ENUM,
                "description": "Effort estimate.",
            },
            "status": {
                "type": "STRING",
                "enum": STATUS_ENUM,
                "description": "Status. Default Open on create. Not 'Deleted': use delete_milestone.",
            },
            "tags": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": (
                    "The milestone's full set of tag names, from list_project_tags. Replaces "
                    "the current tags; [] clears. An unknown name is refused."
                ),
            },
            "assignee_ids": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": (
                    "User ids of ALL the milestone's assignees (get_team_members / "
                    "list_project_members). Replaces the current set: include the ones you "
                    "are keeping. [] clears."
                ),
            },
            "milestone_folder_id": {
                "type": "STRING",
                "description": (
                    "A milestone folder's id (the `folder_id` of a folder in list_milestones' "
                    "`folders`): the folder to file the milestone in, or the folder to "
                    "rename / delete. For update, '' takes the milestone out of its folder. "
                    "Not a note folder."
                ),
            },
            "folder_name": {
                "type": "STRING",
                "description": "Folder name (at most 100 characters). For create_folder and rename_folder.",
            },
            "sprint_id": {
                "type": "STRING",
                "description": (
                    "A sprint of the milestone's project (from list_sprints) to put the "
                    "milestone in; its tasks follow. '' takes the milestone out of its sprint."
                ),
            },
        },
        "required": ["action"],
    },
    run=_run,
    requires_approval=True,
)
