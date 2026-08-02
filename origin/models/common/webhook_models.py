"""Outbound webhooks (readiness plan §4.4).

Two tables: the subscription and the outbox.

## Why an outbox rather than an inline POST

Same reason the email channel has one (`EmailNotificationEvent`): retry
semantics and a no-duplicate guarantee. A receiver being down is the
normal case, not the exception, and an inline send loses those events
silently — which is the first thing every integrator asks about.

Rows are claimed by an atomic
`filter(status=PENDING).update(status=SENDING)`, so overlapping cron
passes cannot double-send, and a crash mid-pass leaves SENDING rows for
the stale sweep rather than losing them.

## What this adds to the email template

`next_attempt_at`. The email outbox retries flat every 5 minutes, which
is fine when the destination is one provider we have a contract with. A
customer's endpoint is not that: it may be down for hours, and hammering
it every tick is both rude and useless. So deliveries carry an explicit
next-attempt time and back off exponentially.

`WebhookEndpoint.consecutive_failures` closes the other end of the same
problem — an endpoint that has failed enough times is disabled, so a
long-dead URL stops consuming cron budget forever.

## The secret is reversible, unlike an API key

`ApiKey` stores a one-way hash because it only ever needs to *verify* a
presented value. A webhook secret must be *used* to sign each payload,
so it is encrypted with the same Fernet helper that protects OAuth
tokens (`services/crypto.py`) rather than hashed.
"""

import secrets
import uuid

from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.services import crypto

# Event names are a public contract — an integrator writes
# `if event == "task.created"`. Renaming one is a breaking change, so
# the list lives here rather than being derived from internal enums that
# are free to churn.
EVENT_TASK_CREATED = "task.created"
EVENT_TASK_UPDATED = "task.updated"
EVENT_TASK_COMPLETED = "task.completed"
EVENT_TASK_COMMENT_CREATED = "task.comment_created"
EVENT_MESSAGE_CREATED = "message.created"

EVENT_CHOICES = [
    (EVENT_TASK_CREATED, EVENT_TASK_CREATED),
    (EVENT_TASK_UPDATED, EVENT_TASK_UPDATED),
    (EVENT_TASK_COMPLETED, EVENT_TASK_COMPLETED),
    (EVENT_TASK_COMMENT_CREATED, EVENT_TASK_COMMENT_CREATED),
    (EVENT_MESSAGE_CREATED, EVENT_MESSAGE_CREATED),
]
ALL_EVENTS = [e for e, _ in EVENT_CHOICES]

# Events scoped by project. An endpoint with a non-empty `project_ids`
# receives these only for the projects it named.
PROJECT_SCOPED_EVENTS = frozenset(
    {
        EVENT_TASK_CREATED,
        EVENT_TASK_UPDATED,
        EVENT_TASK_COMPLETED,
        EVENT_TASK_COMMENT_CREATED,
    }
)

# Events scoped by channel — and, unlike the project ones, an empty
# `channel_ids` does NOT mean "all". See `CHAT_EVENTS_REQUIRE_CHANNELS`.
CHANNEL_SCOPED_EVENTS = frozenset({EVENT_MESSAGE_CREATED})

# Subscribing to chat requires naming the channels explicitly.
#
# For project events, "no filter" sensibly means "the whole team" — the
# subscriber is already trusted with the team's task metadata, and that
# is the shape every integrator expects. Chat is different in kind: the
# payload carries what people actually wrote, and a webhook is configured
# by ONE admin on behalf of everyone who talks in those channels. An
# empty list there would mean "every conversation in the team, including
# ones created after you subscribed", which nobody can meaningfully
# consent to.
#
# DM channels cannot be named at all — see `SUBSCRIBABLE_CHANNEL_KINDS`.
# A private one-to-one is the one place where a team admin's
# configuration choice is clearly not theirs to make.
CHAT_EVENTS_REQUIRE_CHANNELS = True

# ChannelKind values a webhook may subscribe to: GM(2), PM(3), MDM(4).
# DM is 1 and is deliberately absent. Imported lazily at validation time
# to keep this module free of a chat-app import.
SUBSCRIBABLE_CHANNEL_KINDS = frozenset({2, 3, 4})

# After this many consecutive failures the endpoint is disabled. Five
# attempts each with backoff is already most of a day; past that the URL
# is not coming back on its own and someone has to look.
MAX_CONSECUTIVE_FAILURES = 10


def generate_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(32)}"


class WebhookEndpoint(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        related_name="webhook_endpoints",
        to_field="team_id",
    )
    url = models.URLField(max_length=500)
    description = models.CharField(max_length=200, blank=True, default="")
    # Fernet ciphertext — see the module docstring for why this is
    # reversible where an API key's is not.
    secret_encrypted = models.TextField()
    # Subscribed event names. A JSON list rather than an M2M: the set is
    # small, always read whole, and never queried across rows.
    events = models.JSONField(default=list)
    # Scope filters, same JSON-list reasoning as `events`.
    #
    # `project_ids` empty means EVERY project in the team; `channel_ids`
    # empty means NO channel. That asymmetry is deliberate and is the
    # whole privacy design — see `CHAT_EVENTS_REQUIRE_CHANNELS`. Reading
    # them as one uniform "empty = unfiltered" is the mistake this
    # comment exists to prevent.
    project_ids = models.JSONField(default=list)
    channel_ids = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    disabled_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["team", "is_active"])]
        ordering = ["-ts_created_at"]

    def __str__(self) -> str:
        return f"{self.url} ({self.team_id})"

    @property
    def secret(self) -> str:
        return crypto.decrypt(self.secret_encrypted)

    def set_secret(self, raw: str) -> None:
        self.secret_encrypted = crypto.encrypt(raw)

    def subscribes_to(self, event: str, scope: dict | None = None) -> bool:
        """Does this endpoint want `event` for the object `scope` names?

        `scope` is `{"project_id": …, "channel_id": …}`, resolved by the
        emit site rather than assembled here — the enqueue call has the
        object in hand, this method has only ids. One dict rather than
        widening the signature per event type, so adding an event never
        changes this method's shape.

        A `None`/absent scope key means the event is not filterable on
        that axis and passes. That matters for `task.*` on a task whose
        project FK is null: `ProjectMaster` deletion is `SET_NULL`, so an
        orphaned task is a real state, and dropping its events silently
        would repeat the bug `test_get_team_tasks_handles_null_fks`
        already caught once.
        """
        if not self.is_active or event not in (self.events or []):
            return False
        scope = scope or {}

        if event in CHANNEL_SCOPED_EVENTS:
            # Empty = nothing, not everything. An endpoint that somehow
            # subscribed to a chat event without naming channels receives
            # no chat, which is the safe direction to fail.
            channel_id = scope.get("channel_id")
            if channel_id is None:
                return False
            return str(channel_id) in {str(c) for c in (self.channel_ids or [])}

        if event in PROJECT_SCOPED_EVENTS and self.project_ids:
            project_id = scope.get("project_id")
            if project_id is None:
                # An orphaned task in a filtered subscription: the filter
                # names projects and this object is in none of them.
                return False
            return str(project_id) in {str(p) for p in self.project_ids}

        return True


class WebhookDelivery(models.Model):
    """One attempt-tracked delivery of one event to one endpoint."""

    STATUS_PENDING = 0
    STATUS_SENDING = 1
    STATUS_SENT = 2
    STATUS_FAILED = 3

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    endpoint = models.ForeignKey(
        WebhookEndpoint, on_delete=models.CASCADE, related_name="deliveries"
    )
    event = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    status = models.PositiveSmallIntegerField(default=STATUS_PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    # The scheduler reads this, so it is what makes backoff possible at
    # all — without it the claim query can only say "everything pending".
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    # Doubles as the claim stamp, so the stale sweep can tell a row that
    # is genuinely in flight from one whose worker died.
    claimed_at = models.DateTimeField(null=True, blank=True)
    last_status_code = models.IntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            # The claim query: pending, due, oldest first.
            models.Index(fields=["status", "next_attempt_at"]),
            models.Index(fields=["endpoint", "status"]),
        ]
        ordering = ["ts_created_at"]

    def __str__(self) -> str:
        return f"{self.event} → {self.endpoint_id} ({self.status})"
