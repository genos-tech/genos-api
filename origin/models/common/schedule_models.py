"""A member's own schedules (MIP-1147): "09:00 every Monday, run this job".

A `MemberSchedule` says WHEN (the recurrence fields, read in the member's own time
zone) and WHAT (`job`, a key in the job registry, with `params`). A cron tick
(`schedule_tick`) runs the ones that have come due as the member, and hands the
output to the delivery hook. Nothing is fixed: the member edits or removes every
schedule, including the ordinary ones a default would create.

**Private to its owner.** Every read and write goes through `user` AND `team`, no
teammate, admin or team view reaches it, and a row is gone when the account is
deleted (`account_deletion`) and included when it is exported (`account_export`).
`team` scopes where the job looks (a member in several teams has schedules in each);
it never widens who may see the row.

The recurrence is FIELDS, not a cron string: a member picks "weekly, Mon and Wed, 09:00"
and we never have to parse or explain `0 9 * * 1,3`. `next_run_at` is the one derived
column and the only one the tick queries on.
"""

from __future__ import annotations

import uuid

from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser

# Only the model is exported by `from .common.schedule_models import *`: the constants
# below are short generic names (STATUS_*, PAUSE_*) that would shadow other models'.
__all__ = ["MemberSchedule"]

FREQ_DAILY = "daily"
FREQ_WEEKDAYS = "weekdays"
FREQ_WEEKLY = "weekly"
FREQ_MONTHLY = "monthly"
FREQ_ONCE = "once"
FREQUENCY_CHOICES = [
    (FREQ_DAILY, "every day"),
    (FREQ_WEEKDAYS, "every Monday to Friday"),
    (FREQ_WEEKLY, "on chosen days of the week"),
    (FREQ_MONTHLY, "on a day of the month"),
    (FREQ_ONCE, "once"),
]

PAUSE_MANUAL = "manual"
PAUSE_FAILURES = "failures"
PAUSE_LEFT_TEAM = "left_team"
PAUSE_INACTIVE = "inactive"
PAUSE_CHOICES = [
    (PAUSE_MANUAL, "paused by the member"),
    (PAUSE_FAILURES, "paused after repeated failures"),
    (PAUSE_LEFT_TEAM, "the member left the team"),
    (PAUSE_INACTIVE, "the account is inactive"),
]

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED_LATE = "skipped_late"
STATUS_SKIPPED_ACCESS = "skipped_access"
STATUS_CHOICES = [
    (STATUS_OK, "ran"),
    (STATUS_FAILED, "failed"),
    (STATUS_SKIPPED_LATE, "skipped: too late to be useful"),
    (STATUS_SKIPPED_ACCESS, "skipped: the member no longer has access"),
]


class MemberSchedule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
        related_name="member_schedules",
    )
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
        related_name="member_schedules",
    )
    name = models.CharField(max_length=100)
    # A key in `origin.services.schedule_jobs.JOBS`. Not a choices list: the registry is
    # code, and a key that is gone after a deploy must fail one run, not break the model.
    job = models.CharField(max_length=64)
    params = models.JSONField(default=dict, blank=True)

    frequency = models.CharField(max_length=16, choices=FREQUENCY_CHOICES)
    # 0-6, Monday = 0 (Python's `date.weekday()`). Used by `weekly` only.
    weekdays = models.JSONField(default=list, blank=True)
    # 1-31. Used by `monthly` only; a month without that day runs on its last day.
    month_day = models.PositiveSmallIntegerField(null=True, blank=True)
    # The local date. Used by `once` only.
    run_date = models.DateField(null=True, blank=True)
    # Local wall-clock time, minute resolution, in the member's zone.
    time_of_day = models.TimeField()

    enabled = models.BooleanField(default=True)
    paused_reason = models.CharField(max_length=16, choices=PAUSE_CHOICES, blank=True, default="")
    # The zone name `next_run_at` was computed in. The tick compares it with the member's
    # current zone and recomputes when they differ (the member moved or fixed their zone).
    tz_used = models.CharField(max_length=64, blank=True, default="")
    # UTC. Null when the schedule is paused or a one-off has run.
    next_run_at = models.DateTimeField(null=True, blank=True)

    last_run_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=16, choices=STATUS_CHOICES, blank=True, default="")
    # What went wrong, in words we wrote (an exception's type and status), never the text
    # of an exception or anything the job read.
    last_error = models.CharField(max_length=200, blank=True, default="")
    # A summary only: `{"title": ..., "items": <count>}`. The report itself goes to the
    # delivery hook and is not stored here.
    last_result = models.JSONField(default=dict, blank=True)
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    # The tick's claim on a due run (see `services.schedules.run_due`); a claim older than
    # the stale limit is taken over, so a crashed tick does not strand a schedule.
    claimed_at = models.DateTimeField(null=True, blank=True)
    # Unused until the Inbox step: whether a run also sends a push.
    deliver_push = models.BooleanField(default=False)

    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            # The tick's query: enabled rows that are due, oldest first.
            models.Index(fields=["enabled", "next_run_at"], name="member_sched_due_idx"),
            models.Index(fields=["user", "team"], name="member_sched_owner_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.frequency} {self.time_of_day:%H:%M})"
