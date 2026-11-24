"""How long each table keeps data we no longer need (MIP-728).

Every policy here is OFF until someone sets a period. A period is a number of
days in an environment variable, one per policy (`RETENTION_<NAME>_DAYS`), and
the default for every one of them is "keep forever", so deploying this changes
nothing. The periods are a decision about the product and the law, not a
default to guess; this module is the mechanism that lets that decision be a
one-line config change per table.

**"Forever" is a state, not a big number.** `retention_days()` returns `None`
for it, and a policy with `None` is skipped without a query. Unset, empty,
`0`, `forever` and `off` all mean forever, so there is no value that quietly
means "delete everything older than 0 days".

**A purge only touches rows that are finished.** Each policy names the rows it
may take (`eligible`), and that excludes anything still in flight (a `running`
agent run, a `pending` webhook, an unsent email). An age alone is never enough.

**Three kinds of "purge", because the tables differ:**

  * `DELETE`  - the row goes (agent runs, webhook deliveries, email outbox
    rows, task activity).
  * `BLANK`   - the row stays and its content columns are cleared. A message
    stays as a tombstone in its thread (the thread, its `seq` and its reply
    count all depend on the row), and the step of an agent run can lose its
    full tool output while the run keeps its cost and status.
  * `SKIP`    - listed so the decision is on the page: nothing is purged.

Everything runs in batches, newest-first-never: oldest rows first, a bounded
number per pass, so a first run against a big table cannot hold a long lock or
swallow the cron's time budget. `dry_run` counts without writing.

Approved periods (Ken, 2026-11-24, decision D5 on MIP-2200, task MIP-2252). They are
`approved_days` on each policy: `retention_purge --approved-periods` counts them (always a
dry run); the cron applies one only when its `RETENTION_<NAME>_DAYS` variable is set.

  * `task_activity_bodies`  30 d: the PREVIOUS BODY an agent or API-key rewrite kept on a
    `description_edited` row (MIP-738) is cleared, and the row stops saying it can be restored.
  * `task_activities`       365 d: the whole history row goes.
  * `note_versions`          90 d.
  * `webhook_deliveries`     30 d: the payload goes with its row (settled rows only).
  * `agent_step_results`     30 d: a step's tool output AND its summary (a summary can carry an
    address, e.g. from `get_current_user`); the run's own question and answer keep 180 d, and so do its source ids and
    audience (`AgentRun.kept_sources`, no text; MIP-2288), so History keeps its citations.
  * `agent_runs`            180 d, and `agent_sessions` 180 d: a session with no run left loses
    its rolling summary by losing the row.
  * `security_event_network` 90 d: `ip` and `user_agent` are blanked; the event itself keeps
    365 d (`prune_security_events`).
  * Stripe: `redact_deleted_stripe_customers` (service `stripe_retention`), after the 7-year
    bookkeeping period. It calls Stripe, so it is its own command, not a policy here.

Not here, on purpose:

  * `SecurityEvent` rows themselves: only the four high-volume actions have a deleting period
    here (`security_events_high_volume`, MIP-1789), with a floor of 90 days. Every other audit
    row is kept by its own command (`prune_security_events`, 365 days), because an audit trail
    is the one thing a breach investigation cannot do without.
  * `yjs_documents` is written by genos-collab (Node), not Django, so it
    cannot be purged from this process.
"""

from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from django.db import transaction
from django.db.models import Exists, Model, OuterRef, Q, QuerySet
from django.db.models.expressions import RawSQL
from django.utils import timezone

log = logging.getLogger(__name__)

# The most rows one policy touches in one run. A cron that has not run for a
# while, or the first run after a period is set, works through the backlog
# over several runs instead of one long transaction.
MAX_ROWS_PER_RUN = 20_000
_BATCH = 1_000

_FOREVER_WORDS = {"", "0", "forever", "off", "never", "none", "infinite", "inf"}


class Action(enum.Enum):
    DELETE = "delete"
    BLANK = "blank"


def parse_days(raw: str | None) -> int | None:
    """Days from an environment value, or `None` for "keep forever".

    Anything that does not read as a positive whole number is also forever.
    A typo (`3o`) must keep data, not delete it: the failure of a retention
    setting should always be to keep too much, never too little.
    """
    if raw is None:
        return None
    text = raw.strip().lower()
    if text in _FOREVER_WORDS:
        return None
    try:
        days = int(text)
    except ValueError:
        log.warning("retention: ignoring unreadable period %r (keeping forever)", raw)
        return None
    if days <= 0:
        return None
    return days


@dataclass(frozen=True)
class Policy:
    """One table's retention rule.

    `queryset` is a callable, not a queryset, so importing this module does
    not import every model (and so a test can swap it).
    """

    name: str
    description: str
    action: Action
    queryset: Callable[[], QuerySet]
    # The column the age is measured on.
    age_field: str
    # Narrows to rows that may be purged at all, whatever their age.
    eligible: Callable[[], Q] | None = None
    # For BLANK: the columns to clear and what to clear them to.
    blank_to: dict | None = None
    # The period Ken approved (MIP-1786), or None when none is decided. It is
    # NEVER read as a default: only `retention_purge --approved-periods`
    # (always a dry run) uses it, to show what the period would match.
    approved_days: int | None = None
    # The shortest period this policy accepts, whatever the variable says. A
    # value under it is read as this floor (never as "delete sooner").
    floor_days: int | None = None
    # For DELETE rows that the ORM refuses to delete (an append-only table):
    # the function that deletes a batch and returns how many. None means
    # `batch.delete()`.
    remove: Callable[[QuerySet], int] | None = None
    # For BLANK rows that the ORM refuses to update (an append-only table): the
    # function that blanks a batch and returns how many. None means
    # `batch.update(**blank_to)`.
    blank: Callable[[QuerySet], int] | None = None

    @property
    def env_var(self) -> str:
        return f"RETENTION_{self.name.upper()}_DAYS"

    def days(self) -> int | None:
        days = parse_days(os.environ.get(self.env_var))
        if days is None or self.floor_days is None:
            return days
        return max(days, self.floor_days)

    def cutoff(self, now: datetime | None = None) -> datetime | None:
        days = self.days()
        if days is None:
            return None
        return (now or timezone.now()) - timedelta(days=days)

    def candidates(self, now: datetime | None = None, days: int | None = None) -> QuerySet | None:
        """Rows past the period and allowed to go, or `None` when the policy is
        off. Oldest first. `days` overrides the environment (dry-run preview)."""
        if days is None:
            cutoff = self.cutoff(now)
        else:
            if self.floor_days is not None:
                days = max(days, self.floor_days)
            cutoff = (now or timezone.now()) - timedelta(days=days)
        if cutoff is None:
            return None
        qs = self.queryset().filter(**{f"{self.age_field}__lt": cutoff})
        if self.eligible is not None:
            qs = qs.filter(self.eligible())
        return qs.order_by(self.age_field)


@dataclass
class Result:
    name: str
    days: int | None
    matched: int
    changed: int
    dry_run: bool

    @property
    def skipped(self) -> bool:
        return self.days is None


def _agent_runs() -> QuerySet:
    from origin.search_engine.models import AgentRun

    return AgentRun.objects.all()


def _agent_sessions() -> QuerySet:
    from origin.search_engine.models import AgentSession

    return AgentSession.objects.all()


def _agent_steps() -> QuerySet:
    from origin.search_engine.models import AgentStep

    return AgentStep.objects.all()


def _webhook_deliveries() -> QuerySet:
    from origin.models.common.webhook_models import WebhookDelivery

    return WebhookDelivery.objects.all()


def _email_events() -> QuerySet:
    from origin.models.common.notification_models import EmailNotificationEvent

    return EmailNotificationEvent.objects.all()


def _security_events() -> QuerySet:
    from origin.models.common.security_event_models import SecurityEvent

    return SecurityEvent.objects.all()


def _high_volume_security_events() -> Q:
    from origin.services.security_events import HIGH_VOLUME_ACTIONS

    return Q(action__in=HIGH_VOLUME_ACTIONS)


def _remove_security_events(qs: QuerySet) -> int:
    from origin.services.security_events import delete_rows

    return delete_rows(qs)


def _note_versions() -> QuerySet:
    from origin.models.note.version_note_models import NoteVersionMaster

    return NoteVersionMaster.objects.all()


def _task_activities() -> QuerySet:
    from origin.models.task.task_activity_models import TaskActivity

    return TaskActivity.objects.all()


def _workspace_changes() -> QuerySet:
    from origin.models.project.workspace_change_models import WorkspaceChange

    return WorkspaceChange.objects.all()


def _messages() -> QuerySet:
    from origin.models.chat.unified_models import Message

    return Message.objects.all()


def _finished_runs() -> Q:
    # `running` and `awaiting_approval` are in flight: the loop, or a person
    # about to approve a write, still needs the row.
    return ~Q(status__in=("running", "awaiting_approval"))


def _steps_of_finished_runs() -> Q:
    # Still holding something to clear: the tool output, or a summary. A step cleared by an
    # earlier run of the purge matches neither, so a second pass finds nothing.
    return ~Q(run__status__in=("running", "awaiting_approval")) & (
        Q(result_json__isnull=False) | ~Q(summary="")
    )


_STEP_BLANK_TO = {"result_json": None, "summary": ""}


def _keep_sources_then_blank_steps(batch: QuerySet) -> int:
    """Blank a batch of agent steps, after keeping what their runs' answers were built on (MIP-2288).

    The tool output is what History's citations and the shared-answer audit read. Before it goes, each
    run in the batch gets `kept_sources`: its source IDS and flags (no title, snippet or other text)
    and the audience computed from them. Both readers fall back to it, so a run keeps its citations
    and its shared answer for its own 180 days. The sources are written first, so a crash between the
    two leaves a step still holding its output, which the next pass handles.
    """
    from origin.search_engine.agent.controller import id_only_source, reconstruct_sources_for_run
    from origin.search_engine.agent.source_visibility import audit_run
    from origin.search_engine.models import AgentRun

    run_ids = set(batch.values_list("run_id", flat=True))
    with transaction.atomic():
        for run in AgentRun.objects.filter(pk__in=run_ids).prefetch_related("steps"):
            try:
                sources, audience = audit_run(run)
            except Exception:  # noqa: BLE001 - a failed audit must not stop the privacy purge
                # Fail closed: the sources stay, with no audience, so the answer is not collected.
                log.exception("Could not audit agent run %s before blanking its steps", run.pk)
                sources, audience = reconstruct_sources_for_run(run), None
            if not sources:
                continue
            run.kept_sources = {
                "sources": [id_only_source(s) for s in sources],
                "audience": audience,
            }
            run.save(update_fields=["kept_sources"])
        return batch.update(**_STEP_BLANK_TO)


def _sessions_without_runs() -> Q:
    """A session none of whose runs is left (the last one was purged, or it never had one that
    stayed): its rolling summary is the last text of the conversation, so it goes with the row."""
    from origin.search_engine.models import AgentRun

    return ~Exists(AgentRun.objects.filter(session_id=OuterRef("pk")))


def _kept_previous_body() -> Q:
    # `previousBodyKept` is set only on a description row that holds the old text; the purge
    # removes the key, so a cleared row is not counted again.
    return Q(action_type="description_edited", metadata__previousBodyKept=True)


def _history_but_not_a_deleted_tasks_clock() -> Q:
    # `purge_deleted_tasks` dates a deletion by the task's `status_changed` row to "Deleted",
    # and keeps some deleted tasks longer for a stated reason (sub-tasks, the project's highest
    # number). Without that row such a task reads as "no time" and is never purged, so while
    # the task is still Deleted its Deleted row is kept whatever its age.
    return ~Q(
        action_type="status_changed",
        field_name="status",
        new_value="Deleted",
        task__status="Deleted",
    )


def _blank_network_of_security_events() -> Q:
    return Q(ip__isnull=False) | ~Q(user_agent="")


def _blank_security_network(qs: QuerySet) -> int:
    from origin.services.security_events import blank_network_fields

    return blank_network_fields(qs)


def _settled_webhooks() -> Q:
    from origin.models.common.webhook_models import WebhookDelivery as W

    # Sent, or failed for good. Pending and sending rows are still owed a try.
    return Q(status__in=(W.STATUS_SENT, W.STATUS_FAILED))


def _settled_emails() -> Q:
    from origin.models.common.notification_models import EmailNotificationEvent as E

    return Q(status__in=(E.STATUS_SENT, E.STATUS_SKIPPED, E.STATUS_FAILED))


def _deleted_messages() -> Q:
    # Only a message that was deleted, and only one that still has a body to
    # clear (so a second run finds nothing and a cleared tombstone is not
    # rewritten every day).
    return Q(deleted_at__isnull=False) & ~Q(body_text="")


POLICIES: tuple[Policy, ...] = (
    Policy(
        name="agent_runs",
        description="Spotlight agent runs (question, answer, steps, calls) once finished.",
        action=Action.DELETE,
        queryset=_agent_runs,
        age_field="started_at",
        eligible=_finished_runs,
        approved_days=180,
    ),
    Policy(
        name="agent_step_results",
        description=(
            "Full tool output of an agent step. The step and its run stay, "
            "with their timing and cost; the run keeps its source ids and audience, no text."
        ),
        action=Action.BLANK,
        queryset=_agent_steps,
        age_field="created_at",
        eligible=_steps_of_finished_runs,
        blank_to=_STEP_BLANK_TO,
        blank=_keep_sources_then_blank_steps,
        approved_days=30,
    ),
    Policy(
        name="agent_sessions",
        description=(
            "A Spotlight conversation with no run left. Its rolling summary is the last text "
            "of it, and goes with the row."
        ),
        action=Action.DELETE,
        queryset=_agent_sessions,
        age_field="last_active_at",
        eligible=_sessions_without_runs,
        approved_days=180,
    ),
    Policy(
        name="webhook_deliveries",
        description="Outgoing webhook deliveries, with their payloads, once settled.",
        action=Action.DELETE,
        queryset=_webhook_deliveries,
        age_field="ts_created_at",
        eligible=_settled_webhooks,
        approved_days=30,
    ),
    Policy(
        name="email_notification_events",
        description="Email outbox rows (title, body) once sent, skipped or failed.",
        action=Action.DELETE,
        queryset=_email_events,
        age_field="ts_created_at",
        eligible=_settled_emails,
        approved_days=90,
    ),
    Policy(
        name="note_versions",
        description="Saved snapshots of a note. The note itself is untouched.",
        action=Action.DELETE,
        queryset=_note_versions,
        age_field="ts_created_at",
        approved_days=90,
    ),
    Policy(
        name="task_activity_bodies",
        description=(
            "The previous body an agent or API-key rewrite kept on a task's history row "
            "(MIP-738). The row stays; it stops offering a restore."
        ),
        action=Action.BLANK,
        queryset=_task_activities,
        age_field="ts_created_at",
        eligible=_kept_previous_body,
        blank_to={
            "old_value": None,
            # `jsonb - text`: every other key of the metadata (who, via what) stays.
            "metadata": RawSQL("metadata - 'previousBodyKept'", []),
        },
        approved_days=30,
    ),
    Policy(
        name="task_activities",
        description="The change history shown on a task.",
        action=Action.DELETE,
        queryset=_task_activities,
        age_field="ts_created_at",
        eligible=_history_but_not_a_deleted_tasks_clock,
        approved_days=365,
    ),
    Policy(
        name="workspace_changes",
        description=(
            "Who changed a project, a sprint, a label or a tag, and how (MIP-1758): "
            "the row, its actor and its source. Names of changed fields only."
        ),
        action=Action.DELETE,
        queryset=_workspace_changes,
        age_field="created_at",
        approved_days=365,
    ),
    Policy(
        name="security_events_high_volume",
        description=(
            "The security log's api_key and AI-app used / refused rows (one per key or "
            "connection, address and hour). Other security events are not here: they keep "
            "365 days in `prune_security_events`."
        ),
        action=Action.DELETE,
        queryset=_security_events,
        age_field="ts_created_at",
        eligible=_high_volume_security_events,
        approved_days=90,
        floor_days=90,
        remove=_remove_security_events,
    ),
    Policy(
        name="security_event_network",
        description=(
            "The address and browser of a security event. The event (what, when, who, which "
            "team) stays for its own 365 days; only these two columns are cleared."
        ),
        action=Action.BLANK,
        queryset=_security_events,
        age_field="ts_created_at",
        eligible=_blank_network_of_security_events,
        blank_to={"ip": None, "user_agent": ""},
        approved_days=90,
        floor_days=90,
        blank=_blank_security_network,
    ),
    Policy(
        name="deleted_message_bodies",
        description=(
            "The text of a message that was deleted. The row stays as a "
            "tombstone so its thread, order and reply count hold."
        ),
        action=Action.BLANK,
        queryset=_messages,
        age_field="deleted_at",
        eligible=_deleted_messages,
        blank_to={"body": {}, "body_text": ""},
    ),
)

BY_NAME = {p.name: p for p in POLICIES}


def _apply(policy: Policy, qs: QuerySet, limit: int) -> int:
    """Delete or blank up to `limit` rows of `qs`, in batches. Returns how many."""
    pk = policy.queryset().model._meta.pk.name
    done = 0
    while done < limit:
        ids = list(qs.values_list(pk, flat=True)[: min(_BATCH, limit - done)])
        if not ids:
            break
        batch = policy.queryset().filter(**{f"{pk}__in": ids})
        if policy.eligible is not None:
            # Eligibility is checked again at write time, not only when the ids were chosen:
            # a row that changed in between (a session that got a new run, a step whose run
            # started waiting for approval) is left alone, and a batch hook that loops on its
            # own queryset (`blank`) runs out of rows once they are cleared instead of
            # picking the same ones again.
            batch = batch.filter(policy.eligible())
        if policy.action is Action.DELETE:
            if policy.remove is not None:
                policy.remove(batch)
            else:
                batch.delete()
        elif policy.blank is not None:
            policy.blank(batch)
        else:
            batch.update(**policy.blank_to)
        done += len(ids)
        if len(ids) < _BATCH:
            break
    return done


def run_policy(
    policy: Policy, *, dry_run: bool = False, now: datetime | None = None, limit: int | None = None
) -> Result:
    days = policy.days()
    qs = policy.candidates(now)
    if qs is None:
        return Result(policy.name, None, 0, 0, dry_run)
    cap = MAX_ROWS_PER_RUN if limit is None else limit
    if dry_run:
        return Result(policy.name, days, qs.count(), 0, True)
    changed = _apply(policy, qs, cap)
    return Result(policy.name, days, changed, changed, False)


def preview_approved(policy: Policy, *, now: datetime | None = None) -> Result | None:
    """Count what the approved period would match. Read-only, ignores the
    environment, and `None` for a policy with no approved period."""
    if policy.approved_days is None:
        return None
    qs = policy.candidates(now, days=policy.approved_days)
    return Result(policy.name, policy.approved_days, qs.count(), 0, True)


def run_all(
    *, only: str | None = None, dry_run: bool = False, now: datetime | None = None
) -> list[Result]:
    if only is not None and only not in BY_NAME:
        raise KeyError(only)
    chosen = [BY_NAME[only]] if only else list(POLICIES)
    return [run_policy(p, dry_run=dry_run, now=now) for p in chosen]


def model_of(policy: Policy) -> type[Model]:
    return policy.queryset().model
