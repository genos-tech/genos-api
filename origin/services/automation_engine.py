"""Running a project's automation rules (MIP-2498).

**Capture, then act later.** The task signals call `offer` for a change a rule may react to. It
writes a `pending` `AutomationRun` in the same transaction and remembers the task. Nothing else
happens yet: the writer (a view that saves a task several times, a tool, a webhook) finishes first,
then `flush` evaluates the task as it ended up. `flush` is called by the task PUT before it answers,
at the end of every other request (`request_finished`), at the end of an agent worker (`batch`), and
for anything left behind by the schedule tick (`sweep`). Running at `on_commit` of the save would
be too early: the PUT's later steps would write over what a rule did.

**Once.** A run leaves `pending` under a row lock, together with the action, in one transaction.

**One hop.** The engine's own writes are never captured (`suppress_automation`), so no rule reacts
to a rule, and a task moved from A to B is not looked at by B's rules for that change. A per-task
rate limit stops people and rules bouncing a task between projects.

**What does not fire:** queryset cascades (sub-tasks riding a move, milestone and sprint moves,
import undo: they send no `post_save`), the automatic Open/Blocked flip, imports, an agent write's
undo, turning a task into a milestone, and unfinished drafts.

**Milestones** (MIP-2521) have their own rules (`applies_to`). Their changes are captured from
`MilestoneMaster` (`offer_milestone`), their runs hang off the backing task, and what differs
when acting is in `automation_milestones`.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta

from django.core.signals import request_finished, request_started
from django.db import transaction
from django.utils import timezone

from origin.middleware.current_user import acting_as, get_current_source
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.automation_models import (
    AutomationRule,
    AutomationRun,
    AutomationRunState,
    AutomationTrigger,
)
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.services import automation_milestones as milestones
from origin.services import automation_rules as rules
from origin.services.activity_suppression import activity_suppressed
from origin.services.automation_notice import post_move_notice
from origin.services.task_assignment_notice import (
    notify_task_assigned,
    notify_task_moved_by_rule,
)
from origin.services.task_cache import invalidate_for_task
from origin.services.task_project_move import (
    collect_descendant_task_ids,
    task_move_assignee_problem,
)
from origin.services.task_reparent import is_nested_task, move_task_to_project, project_move_error

log = logging.getLogger(__name__)

#: Applied runs one task may have inside `RATE_WINDOW`.
RATE_LIMIT = 3
RATE_WINDOW = timedelta(hours=1)
#: A change this old is not acted on any more.
STALE_AFTER = timedelta(hours=24)
#: Tasks one request acts on before it answers or ends; the sweep takes the rest.
INLINE_LIMIT = 20
#: How long a pending run waits before the sweep takes it for abandoned.
SWEEP_AFTER = timedelta(seconds=60)

_suppressed: ContextVar[bool] = ContextVar("automation_suppressed", default=False)
_queued: ContextVar[dict | None] = ContextVar("automation_queued", default=None)


@contextmanager
def suppress_automation():
    """Writes in this block set off no rule (the engine's own, and bulk loads)."""
    token = _suppressed.set(True)
    try:
        yield
    finally:
        _suppressed.reset(token)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def offer(task: TaskMaster, kind: str, payload: dict | None = None) -> None:
    """A change of `task` a rule may react to. Best effort: never fails the write."""
    try:
        if _suppressed.get() or activity_suppressed():
            return
        if task.is_init_task or task.is_milestone or task.is_deleted or task.project_id is None:
            return
        if (get_current_source() or {}).get("undo_of"):
            return
        # A savepoint, so a failure here cannot poison the writer's transaction.
        with transaction.atomic():
            if not _has_rules(task.project_id, rules.APPLIES_TO_TASK, kind):
                return
            AutomationRun.objects.create(
                task_id=task.task_id,
                source_project_id=task.project_id,
                trigger_type=kind,
                payload=payload or {},
            )
        _remember(task.task_id)
    except Exception:  # noqa: BLE001 - an observer must not fail the task write
        log.warning("automation capture failed task=%s", task.pk, exc_info=True)


def offer_milestone(milestone, kind: str, payload: dict | None = None) -> None:
    """A change of `milestone` (a `MilestoneMaster`) a rule may react to. Best effort."""
    try:
        if _suppressed.get() or activity_suppressed():
            return
        if milestone.is_deleted or milestone.task_id is None or milestone.project_id is None:
            return
        if (get_current_source() or {}).get("undo_of"):
            return
        with transaction.atomic():
            if not _has_rules(milestone.project_id, rules.APPLIES_TO_MILESTONE, kind):
                return
            AutomationRun.objects.create(
                task_id=milestone.task_id,
                source_project_id=milestone.project_id,
                trigger_type=kind,
                payload={**(payload or {}), "milestone_id": milestone.milestone_id},
            )
        _remember(milestone.task_id, is_milestone=True)
    except Exception:  # noqa: BLE001 - an observer must not fail the milestone write
        log.warning("automation capture failed milestone=%s", milestone.pk, exc_info=True)


def has_milestone_rules(project_id) -> bool:
    """Is there anything a milestone change of this project could set off? Asked before the
    milestone signals read the previous row."""
    return AutomationRule.objects.filter(
        project_id=project_id, enabled=True, applies_to=rules.APPLIES_TO_MILESTONE
    ).exists()


def _has_rules(project_id, applies_to: str, kind: str) -> bool:
    return AutomationRule.objects.filter(
        project_id=project_id, enabled=True, applies_to=applies_to, trigger_type=kind
    ).exists()


def _remember(task_id, *, is_milestone: bool = False) -> None:
    queued = _queued.get()
    if queued is None:
        queued = {}
        _queued.set(queued)
    queued[task_id] = is_milestone


# ---------------------------------------------------------------------------
# When to run
# ---------------------------------------------------------------------------


def flush() -> dict:
    """Act on every change this request or worker has captured. `{task_id: Outcome}` for the
    tasks a rule matched. Never raises."""
    queued = _queued.get()
    if not queued:
        return {}
    _queued.set(None)
    outcomes = {}
    milestone_budget = milestones.INLINE_LIMIT
    for task_id, is_milestone in list(queued.items())[:INLINE_LIMIT]:
        if is_milestone:
            if milestone_budget <= 0:
                continue  # stays pending: the sweep takes it
            milestone_budget -= 1
        try:
            if (outcome := process_task(task_id)) is not None:
                outcomes[task_id] = outcome
        except Exception:  # noqa: BLE001 - the change is saved; the sweep tries again
            log.exception("automation run failed task=%s", task_id)
    return outcomes


@contextmanager
def batch():
    """Run what a block captured when it ends: for a thread that serves no request."""
    token = _queued.set(None)
    try:
        yield
    finally:
        try:
            flush()
        finally:
            _queued.reset(token)


def sweep(limit: int = 200) -> int:
    """Pending runs nobody flushed (a worker that died, a command that wrote tasks)."""
    ids = list(
        AutomationRun.objects.filter(
            state=AutomationRunState.PENDING, ts_created_at__lt=timezone.now() - SWEEP_AFTER
        )
        .values_list("task_id", flat=True)
        .distinct()[:limit]
    )
    done = 0
    for task_id in ids:
        try:
            process_task(task_id)
            done += 1
        except Exception:  # noqa: BLE001
            log.exception("automation sweep failed task=%s", task_id)
    return done


def _on_request_started(**kwargs) -> None:
    _queued.set(None)


def _on_request_finished(**kwargs) -> None:
    flush()


request_started.connect(_on_request_started, dispatch_uid="automation_request_started")
request_finished.connect(_on_request_finished, dispatch_uid="automation_request_finished")


# ---------------------------------------------------------------------------
# Acting
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    state: str
    reason: str = ""
    rule_id: int | None = None
    destination_project_id: int | None = None
    #: A repeat that is not worth another history line or run row.
    quiet: bool = False


def process_task(task_id) -> Outcome | None:
    """Apply the first matching rule to a task's pending changes (or, for a milestone's backing
    task, to the milestone's). None when no rule matched or there was nothing to do (including:
    another worker holds the runs)."""
    with transaction.atomic():
        runs = list(
            AutomationRun.objects.select_for_update(skip_locked=True)
            .filter(task_id=task_id, state=AutomationRunState.PENDING)
            .order_by("pk")
        )
        if not runs:
            return None
        pending_ids = [r.pk for r in runs]
        milestone = None
        tasks = TaskMaster.objects.select_related("project").filter(task_id=task_id)
        if tasks.filter(is_milestone=True).exists():
            # The milestone row is the one locked (as a move by hand does), not the backing task.
            milestone = milestones.lock(task_id)
            task = tasks.first()
            rule, run = _first_milestone_match(milestone, runs)
        else:
            task = tasks.select_for_update(of=("self",)).first()
            rule, run = _first_match(task, runs)
        if rule is None:
            AutomationRun.objects.filter(pk__in=pending_ids).delete()
            return None
        outcome = _act(rule, task, milestone)
        if outcome.quiet:
            AutomationRun.objects.filter(pk__in=pending_ids).delete()
            return outcome
        AutomationRun.objects.filter(pk=run.pk).update(
            rule=rule,
            state=outcome.state,
            reason=outcome.reason,
            destination_project_id=outcome.destination_project_id,
            ts_done_at=timezone.now(),
        )
        AutomationRun.objects.filter(pk__in=pending_ids).exclude(pk=run.pk).delete()
        return outcome


def _first_match(task, runs):
    """`(rule, run)`: the first rule, in order, that one of the changes sets off and whose
    conditions hold on the task as it is now."""
    if task is None or task.is_deleted or task.is_init_task or task.is_milestone:
        return None, None
    if task.status == "Deleted":
        return None, None
    # A milestone's changes are not the task's (a backing row turned into a task keeps them).
    runs = [r for r in runs if "milestone_id" not in r.payload]
    return _match(task.project, rules.APPLIES_TO_TASK, runs, lambda: task)


def _first_milestone_match(milestone, runs):
    if milestone is None or milestone.status == "Deleted":
        return None, None
    runs = [r for r in runs if r.payload.get("milestone_id") == milestone.milestone_id]
    return _match(
        milestone.project,
        rules.APPLIES_TO_MILESTONE,
        runs,
        lambda: milestones.subject_of(milestone),
    )


def _match(project, applies_to: str, runs: list, subject_of):
    if project is None or project.is_deleted or project.archived_at is not None:
        return None, None
    fresh = timezone.now() - STALE_AFTER
    # A change made in a project the task has since left is not this project's business.
    runs = [
        r for r in runs if r.source_project_id == project.project_id and r.ts_created_at > fresh
    ]
    if not runs:
        return None, None
    subject, refs = subject_of(), None
    for rule in (
        AutomationRule.objects.filter(
            project_id=project.project_id, enabled=True, applies_to=applies_to
        )
        .select_related("destination_project", "set_custom_status", "set_assignee", "owner")
        .order_by("position", "id")
    ):
        run = next(
            (r for r in runs if rules.trigger_matches(rule, r.trigger_type, r.payload, subject)),
            None,
        )
        if run is None:
            continue
        refs = refs or rules.ProjectRefs(project.project_id)
        if rules.conditions_hold(rule, subject, refs):
            return rule, run
    return None, None


def rule_problem(rule: AutomationRule, source: ProjectMaster) -> str | None:
    """The guards that do not depend on the task: why the rule cannot act at all, or None."""
    owner = rule.owner
    if owner is not None and not rules.plan_allows(owner.id):
        return "plan"
    destination = rule.destination_project
    if rule.moves_task and destination is None:
        return "destination_not_found"
    if problem := rules.owner_problem(owner, source, destination):
        return problem
    if rule.moves_task and (problem := rules.destination_problem(source, destination)):
        return problem
    target = destination if rule.moves_task else source
    if rule.set_assignee_id and not (
        rule.set_assignee.is_active and rules.assignee_allowed(target, rule.set_assignee_id)
    ):
        return "assignee_not_allowed"
    custom = rule.set_custom_status
    if custom is not None and (
        custom.project_id != target.project_id
        or custom.archived_at is not None
        or custom.category != rule.set_status
    ):
        return "bad_status"
    return None


def task_problem(rule: AutomationRule, task: TaskMaster) -> tuple[str, str]:
    """`(state, reason)` of the guards about this task; state is "applied" when all pass."""
    refused = AutomationRunState.REFUSED
    if rule.moves_task:
        destination = rule.destination_project
        # A sub-task follows its parent; the rule is not half-applied to it.
        if is_nested_task(task):
            return AutomationRunState.SKIPPED, "nested"
        if project_move_error(task, destination.project_id, task.project.team_id, rule.owner_id):
            return refused, "move_not_allowed"
        if task_move_assignee_problem(task, destination, assignee=rule.set_assignee_id):
            return refused, "assignee_not_on_destination"
    return _rate_check(task.task_id, RATE_LIMIT)


def milestone_problem(rule: AutomationRule, milestone) -> tuple[str, str]:
    """`task_problem` for a milestone rule."""
    if rule.moves_task and (problem := milestones.move_problem(rule, milestone)):
        return AutomationRunState.REFUSED, problem
    return _rate_check(milestone.task_id, milestones.RATE_LIMIT)


def _rate_check(task_id, limit: int) -> tuple[str, str]:
    recent = AutomationRun.objects.filter(
        task_id=task_id,
        state=AutomationRunState.APPLIED,
        ts_done_at__gte=timezone.now() - RATE_WINDOW,
    ).count()
    if recent >= limit:
        return AutomationRunState.REFUSED, "rate_limited"
    return AutomationRunState.APPLIED, ""


def check(rule: AutomationRule, task: TaskMaster, milestone=None) -> tuple[str, str]:
    """`(state, reason)` the rule would end in on `task` (or `milestone`) now, writing nothing."""
    project = milestone.project if milestone is not None else task.project
    if problem := rule_problem(rule, project):
        return AutomationRunState.REFUSED, problem
    if milestone is not None:
        return milestone_problem(rule, milestone)
    return task_problem(rule, task)


def _act(rule: AutomationRule, task: TaskMaster, milestone=None) -> Outcome:
    """`task` is what the history is written on: the milestone's backing row for a milestone."""
    source_id = milestone.project_id if milestone is not None else task.project_id
    destination_id = rule.destination_project_id if rule.moves_task else None
    state, reason = check(rule, task, milestone)
    if state == AutomationRunState.APPLIED:
        try:
            with transaction.atomic():  # a savepoint: a failed action leaves nothing behind
                reason = (
                    _apply(rule, task)
                    if milestone is None
                    else _apply_milestone(rule, milestone, task)
                )
            if reason == "no_change":
                return Outcome(AutomationRunState.SKIPPED, reason, rule.id)
        except Exception:  # noqa: BLE001
            log.exception("automation rule=%s failed on task=%s", rule.id, task.task_id)
            state, reason = AutomationRunState.REFUSED, "error"
            task.refresh_from_db()
            if milestone is not None:
                milestone.refresh_from_db()
    if state != AutomationRunState.APPLIED:
        if reason in rules.OWNER_PROBLEMS or (rule.moves_task and rule.destination_project is None):
            AutomationRule.objects.filter(pk=rule.pk).update(
                enabled=False, disabled_reason=reason, ts_updated_at=timezone.now()
            )
        if reason == "rate_limited" and _already_limited(task):
            return Outcome(state, reason, rule.id, quiet=True)
        # The history is read by everyone who reads the task; the runs log keeps the exact code.
        _record(
            rule,
            task,
            TaskActivityActionType.AUTOMATION_REFUSED,
            {"reason": rules.public_reason(reason)},
        )
        return Outcome(state, reason, rule.id)
    _record(
        rule,
        task,
        TaskActivityActionType.AUTOMATION_APPLIED,
        {
            "fromProjectId": source_id,
            "toProjectId": destination_id,
            "trigger": rule.trigger_type,
            **({"milestoneId": milestone.milestone_id} if milestone is not None else {}),
        },
    )
    if destination_id is not None and destination_id != source_id:
        post_move_notice(
            task_id=task.task_id,
            rule_name=rule.name,
            source_project_id=source_id,
            destination_project_id=destination_id,
        )
    return Outcome(state, "", rule.id, destination_id)


def _already_limited(task) -> bool:
    return AutomationRun.objects.filter(
        task_id=task.task_id,
        state=AutomationRunState.REFUSED,
        reason="rate_limited",
        ts_done_at__gte=timezone.now() - RATE_WINDOW,
    ).exists()


def _record(rule, task, action, extra: dict) -> None:
    from origin.signals.task_signals import record_activity  # noqa: PLC0415 - cycle

    # Ids only, no project names: whoever reads the history may not be on the other project.
    record_activity(
        task=task,
        action_type=action,
        field_name="automation",
        metadata={
            "ruleId": rule.id,
            "ruleName": rule.name,
            "via": {"kind": "rule", "rule_id": rule.id},
            **extra,
        },
        actor=rule.owner,
    )


def _remap_tags(names: list[str], project_id, folded: dict) -> list[dict]:
    """The destination's tags that carry the names the task had; a name it lacks is dropped."""
    out = []
    for name in names:
        tag = rules.find_tag(project_id, name, folded)
        if tag is not None and tag not in out:
            out.append(tag)
    return out


def _apply(rule: AutomationRule, task: TaskMaster) -> str:
    """Do the rule's actions as its owner. "" or "no_change"."""
    owner = rule.owner
    with suppress_automation(), acting_as(owner, {"kind": "rule", "rule_id": rule.id}):
        previous_assignee = task.assignee_id
        changed: set[str] = set()
        if rule.moves_task:
            moving = {task.task_id} | collect_descendant_task_ids(task.task_id)
            names = {
                row.task_id: rules.tag_names_of(row)
                for row in TaskMaster.objects.filter(task_id__in=moving).only("task_id", "tags")
            }
            move_task_to_project(task, rule.destination_project_id)
            task.refresh_from_db()
            folded = rules.catalogue(task.project_id)
            for row in TaskMaster.objects.filter(task_id__in=moving):
                wanted = list(names.get(row.task_id, []))
                if row.task_id == task.task_id and rule.add_tag:
                    wanted.append(rule.add_tag)
                if tags := _remap_tags(wanted, task.project_id, folded):
                    row.tags = tags
                    row.save(update_fields=["tags", "ts_updated_at"])
            task.refresh_from_db()
        elif rule.add_tag:
            have = {t.casefold() for t in rules.tag_names_of(task)}
            tag = rules.find_tag(task.project_id, rule.add_tag)
            if tag is not None and tag["tagName"].casefold() not in have:
                task.tags = [*(task.tags or []), tag]
                changed.add("tags")

        if rule.set_status:
            custom_id = rule.set_custom_status_id
            if task.status != rule.set_status or (
                custom_id is not None and task.custom_status_id != custom_id
            ):
                task.status = rule.set_status
                task.custom_status_id = custom_id
                changed |= {"status", "custom_status"}
        if rule.set_assignee_id and task.assignee_id != rule.set_assignee_id:
            task.assignee = rule.set_assignee
            changed.add("assignee")
        if changed:
            task.save(update_fields=[*changed, "ts_updated_at"])
        if "assignee" in changed:
            notify_task_assigned(task, actor_id=owner.id, previous_assignee_id=previous_assignee)
        elif rule.moves_task:
            notify_task_moved_by_rule(task, actor_id=owner.id)
        if not (rule.moves_task or changed):
            return "no_change"
        task.refresh_from_db()
        invalidate_for_task(task)
    return ""


def _apply_milestone(rule: AutomationRule, milestone, backing: TaskMaster) -> str:
    with suppress_automation(), acting_as(rule.owner, {"kind": "rule", "rule_id": rule.id}):
        reason = milestones.apply(rule, milestone)
    backing.refresh_from_db()
    return reason


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

DRY_RUN_SCAN = 2000
DRY_RUN_ITEMS = 50


def dry_run(rule: AutomationRule, project: ProjectMaster) -> dict:
    """Which of the project's current tasks (or milestones) `rule` would act on: those in the
    state its trigger names that meet its conditions. Writes nothing; `rule` may be unsaved."""
    if rule.applies_to == rules.APPLIES_TO_MILESTONE:
        return _milestone_dry_run(rule, project)
    refs = rules.ProjectRefs(project.project_id)
    tasks = (
        TaskMaster.objects.select_related("project")
        .defer("content", "links", "mentioned_user_ids")
        .filter(project=project, is_deleted=False, is_init_task=False, is_milestone=False)
        .exclude(status="Deleted")
        .order_by("-ts_updated_at", "-task_id")[: DRY_RUN_SCAN + 1]
    )
    tasks = list(tasks)
    truncated = len(tasks) > DRY_RUN_SCAN
    matched = [
        t
        for t in tasks[:DRY_RUN_SCAN]
        if rules.trigger_state_holds(rule, t) and rules.conditions_hold(rule, t, refs)
    ]
    items = []
    # Once for the rule, not once per task.
    blocked = rule_problem(rule, project) if matched else None
    for task in matched[:DRY_RUN_ITEMS]:
        if blocked:
            state, reason = AutomationRunState.REFUSED, blocked
        else:
            state, reason = task_problem(rule, task)
        items.append(
            {
                "taskId": task.task_id,
                "displayId": task.display_id,
                "title": task.title,
                "status": task.status,
                "outcome": "would_apply" if state == AutomationRunState.APPLIED else str(state),
                "reason": reason,
            }
        )
    return {"matched": len(matched), "items": items, "truncated": truncated}


def _milestone_dry_run(rule: AutomationRule, project: ProjectMaster) -> dict:
    refs = rules.ProjectRefs(project.project_id)
    rows = list(
        MilestoneMaster.objects.select_related("project", "task", "task__project")
        .prefetch_related("milestone_assignees")
        .defer("description", "task__content")
        .filter(project=project, is_deleted=False, task__isnull=False)
        .exclude(status="Deleted")
        .order_by("-ts_updated_at", "-milestone_id")[: DRY_RUN_SCAN + 1]
    )
    truncated = len(rows) > DRY_RUN_SCAN
    matched = []
    for row in rows[:DRY_RUN_SCAN]:
        subject = milestones.subject_of(row, [a.user_id for a in row.milestone_assignees.all()])
        if rules.trigger_state_holds(rule, subject) and rules.conditions_hold(rule, subject, refs):
            matched.append(row)
    blocked = rule_problem(rule, project) if matched else None
    items = []
    for row in matched[:DRY_RUN_ITEMS]:
        if blocked:
            state, reason = AutomationRunState.REFUSED, blocked
        else:
            state, reason = milestone_problem(rule, row)
        items.append(
            {
                "milestoneId": row.milestone_id,
                "taskId": row.task_id,
                "displayId": row.task.display_id,
                "title": row.title,
                "status": row.status,
                "outcome": "would_apply" if state == AutomationRunState.APPLIED else str(state),
                "reason": reason,
            }
        )
    return {"matched": len(matched), "items": items, "truncated": truncated}


__all__ = [
    "AutomationTrigger",
    "Outcome",
    "batch",
    "check",
    "dry_run",
    "flush",
    "has_milestone_rules",
    "offer",
    "offer_milestone",
    "process_task",
    "suppress_automation",
    "sweep",
]
