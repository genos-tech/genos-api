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
undo, milestones and unfinished drafts.
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
from origin.models.task.task_activity_models import TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.services import automation_rules as rules
from origin.services.activity_suppression import activity_suppressed
from origin.services.task_assignment_notice import notify_task_assigned
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
            if not AutomationRule.objects.filter(
                project_id=task.project_id, enabled=True, trigger_type=kind
            ).exists():
                return
            AutomationRun.objects.create(
                task_id=task.task_id,
                source_project_id=task.project_id,
                trigger_type=kind,
                payload=payload or {},
            )
        queued = _queued.get()
        if queued is None:
            queued = {}
            _queued.set(queued)
        queued[task.task_id] = True
    except Exception:  # noqa: BLE001 - an observer must not fail the task write
        log.warning("automation capture failed task=%s", task.pk, exc_info=True)


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
    for task_id in list(queued)[:INLINE_LIMIT]:
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
    """Apply the first matching rule to a task's pending changes. None when no rule matched or
    there was nothing to do (including: another worker holds the runs)."""
    with transaction.atomic():
        runs = list(
            AutomationRun.objects.select_for_update(skip_locked=True)
            .filter(task_id=task_id, state=AutomationRunState.PENDING)
            .order_by("pk")
        )
        if not runs:
            return None
        pending_ids = [r.pk for r in runs]
        task = (
            TaskMaster.objects.select_for_update(of=("self",))
            .select_related("project")
            .filter(task_id=task_id)
            .first()
        )
        rule, run = _first_match(task, runs)
        if rule is None:
            AutomationRun.objects.filter(pk__in=pending_ids).delete()
            return None
        outcome = _act(rule, task)
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
    project = task.project
    if (
        project is None
        or project.is_deleted
        or project.archived_at is not None
        or task.status == "Deleted"
    ):
        return None, None
    fresh = timezone.now() - STALE_AFTER
    # A change made in a project the task has since left is not this project's business.
    runs = [
        r for r in runs if r.source_project_id == project.project_id and r.ts_created_at > fresh
    ]
    if not runs:
        return None, None
    refs = None
    for rule in (
        AutomationRule.objects.filter(project_id=project.project_id, enabled=True)
        .select_related("destination_project", "set_custom_status", "set_assignee", "owner")
        .order_by("position", "id")
    ):
        run = next(
            (r for r in runs if rules.trigger_matches(rule, r.trigger_type, r.payload, task)), None
        )
        if run is None:
            continue
        refs = refs or rules.ProjectRefs(project.project_id)
        if rules.conditions_hold(rule, task, refs):
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
    recent = AutomationRun.objects.filter(
        task_id=task.task_id,
        state=AutomationRunState.APPLIED,
        ts_done_at__gte=timezone.now() - RATE_WINDOW,
    ).count()
    if recent >= RATE_LIMIT:
        return refused, "rate_limited"
    return AutomationRunState.APPLIED, ""


def check(rule: AutomationRule, task: TaskMaster) -> tuple[str, str]:
    """`(state, reason)` the rule would end in on `task` now, writing nothing."""
    if problem := rule_problem(rule, task.project):
        return AutomationRunState.REFUSED, problem
    return task_problem(rule, task)


def _act(rule: AutomationRule, task: TaskMaster) -> Outcome:
    source_id = task.project_id
    destination_id = rule.destination_project_id if rule.moves_task else None
    state, reason = check(rule, task)
    if state == AutomationRunState.APPLIED:
        try:
            with transaction.atomic():  # a savepoint: a failed action leaves nothing behind
                reason = _apply(rule, task)
            if reason == "no_change":
                return Outcome(AutomationRunState.SKIPPED, reason, rule.id)
        except Exception:  # noqa: BLE001
            log.exception("automation rule=%s failed on task=%s", rule.id, task.task_id)
            state, reason = AutomationRunState.REFUSED, "error"
            task.refresh_from_db()
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
        {"fromProjectId": source_id, "toProjectId": destination_id, "trigger": rule.trigger_type},
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
        if not (rule.moves_task or changed):
            return "no_change"
        task.refresh_from_db()
        invalidate_for_task(task)
    return ""


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

DRY_RUN_SCAN = 2000
DRY_RUN_ITEMS = 50


def dry_run(rule: AutomationRule, project: ProjectMaster) -> dict:
    """Which of the project's current tasks `rule` would act on: those in the state its trigger
    names that meet its conditions. Writes nothing; `rule` may be unsaved."""
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


__all__ = [
    "AutomationTrigger",
    "Outcome",
    "batch",
    "check",
    "dry_run",
    "flush",
    "offer",
    "process_task",
    "suppress_automation",
    "sweep",
]
