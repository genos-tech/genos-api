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

Not here, on purpose:

  * `SecurityEvent` has its own command (`prune_security_events`) with a floor
    of 90 days, because an audit trail is the one thing a breach investigation
    cannot do without.
  * `yjs_documents` is written by genos-collab (Node), not Django, so it
    cannot be purged from this process.
  * `TaskActivity` stores a description edit as a bare "edited" event with no
    body diff (`task_signals.py`), so there is no description text in that
    table to blank. Its title and custom-field `old_value` / `new_value` are
    what a period for it would remove, along with the rest of the row.
"""

from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from django.db.models import Model, Q, QuerySet
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

    @property
    def env_var(self) -> str:
        return f"RETENTION_{self.name.upper()}_DAYS"

    def days(self) -> int | None:
        return parse_days(os.environ.get(self.env_var))

    def cutoff(self, now: datetime | None = None) -> datetime | None:
        days = self.days()
        if days is None:
            return None
        return (now or timezone.now()) - timedelta(days=days)

    def candidates(self, now: datetime | None = None) -> QuerySet | None:
        """Rows past the period and allowed to go, or `None` when the policy is
        off. Oldest first."""
        cutoff = self.cutoff(now)
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


def _agent_steps() -> QuerySet:
    from origin.search_engine.models import AgentStep

    return AgentStep.objects.all()


def _webhook_deliveries() -> QuerySet:
    from origin.models.common.webhook_models import WebhookDelivery

    return WebhookDelivery.objects.all()


def _email_events() -> QuerySet:
    from origin.models.common.notification_models import EmailNotificationEvent

    return EmailNotificationEvent.objects.all()


def _note_versions() -> QuerySet:
    from origin.models.note.version_note_models import NoteVersionMaster

    return NoteVersionMaster.objects.all()


def _task_activities() -> QuerySet:
    from origin.models.task.task_activity_models import TaskActivity

    return TaskActivity.objects.all()


def _messages() -> QuerySet:
    from origin.models.chat.unified_models import Message

    return Message.objects.all()


def _finished_runs() -> Q:
    # `running` and `awaiting_approval` are in flight: the loop, or a person
    # about to approve a write, still needs the row.
    return ~Q(status__in=("running", "awaiting_approval"))


def _steps_of_finished_runs() -> Q:
    return ~Q(run__status__in=("running", "awaiting_approval")) & Q(result_json__isnull=False)


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
    ),
    Policy(
        name="agent_step_results",
        description=(
            "Full tool output of an agent step. The step and its run stay, "
            "with their summary, timing and cost."
        ),
        action=Action.BLANK,
        queryset=_agent_steps,
        age_field="created_at",
        eligible=_steps_of_finished_runs,
        blank_to={"result_json": None},
    ),
    Policy(
        name="webhook_deliveries",
        description="Outgoing webhook deliveries, with their payloads, once settled.",
        action=Action.DELETE,
        queryset=_webhook_deliveries,
        age_field="ts_created_at",
        eligible=_settled_webhooks,
    ),
    Policy(
        name="email_notification_events",
        description="Email outbox rows (title, body) once sent, skipped or failed.",
        action=Action.DELETE,
        queryset=_email_events,
        age_field="ts_created_at",
        eligible=_settled_emails,
    ),
    Policy(
        name="note_versions",
        description="Saved snapshots of a note. The note itself is untouched.",
        action=Action.DELETE,
        queryset=_note_versions,
        age_field="ts_created_at",
    ),
    Policy(
        name="task_activities",
        description="The change history shown on a task.",
        action=Action.DELETE,
        queryset=_task_activities,
        age_field="ts_created_at",
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
        if policy.action is Action.DELETE:
            batch.delete()
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


def run_all(
    *, only: str | None = None, dry_run: bool = False, now: datetime | None = None
) -> list[Result]:
    if only is not None and only not in BY_NAME:
        raise KeyError(only)
    chosen = [BY_NAME[only]] if only else list(POLICIES)
    return [run_policy(p, dry_run=dry_run, now=now) for p in chosen]


def model_of(policy: Policy) -> type[Model]:
    return policy.queryset().model
