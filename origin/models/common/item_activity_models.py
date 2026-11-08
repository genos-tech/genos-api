"""Per-day activity of one item, for the member it describes (MIP-1084).

`MemberItemDay` is one row per member, team, task and local day: HOW MANY saved actions
happened on that task that day, by kind. Counts only: no text, no title, no time spent. It
is the "per-day activity table" that lets the secretary say an item is rising or cooling,
and it is what outlives the raw ledger (`MemberEvent` keeps 180 days, this keeps a year).

**Derived, not a second source of truth.** Every count is made from rows that already exist
(task history, comments, reactions, thread messages, task-note versions, merged PRs) plus
the member's own `MemberEvent` rows, by the same attachment and access rules as
`services/item_lines.py`, and a row can be rebuilt from them at any time
(`services/item_activity.rebuild`). Weekly sums and the rising / cooling call are computed
on read from these rows and never stored.

Columns, one per kind of saved action (an event counts in exactly one):

  * `mine`    : the member's own changes, comments and reactions on the task, and their own
                changes `MemberEvent` holds (collaborators, dependencies);
  * `others`  : other people's changes, comments and reactions (automation echoes are not
                counted: they are consequences, not attention);
  * `notes`   : edits of the task's notes (by anyone);
  * `chat`    : messages in the chat thread linked to the task, or that point at it;
  * `prs`     : PR events (linked, commented, unlinked, merged).

**Whole local days only.** `day` is the member's own date (their time zone at build time).
Today is never stored: a half day would make a rebuild differ from what was written.

**Private to the member it describes.** Every read filters on `user`. A row whose task the
member cannot open now is dropped before anything is summed or shown.

`MemberItemDayState` is the fill watermark, `built_through`: a day with no activity has no
row, so without it a quiet day would look unbuilt.

Opt-out (`CustomUser.member_events_enabled`) deletes both tables' rows of the member, in the
same transaction as the flag; the writer writes nothing for a member who is off. Kept
`item_activity.RETENTION` (365 days); purged with the account and exported with it.
"""

from __future__ import annotations

from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser

COUNT_FIELDS = ("mine", "others", "notes", "chat", "prs")


class MemberItemDay(models.Model):
    row_id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
        related_name="item_activity_days",
    )
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
        related_name="+",
    )
    # A plain integer, not a foreign key: the row is a record of a day and a rebuild must
    # not depend on when a task was deleted. A task the member can no longer open is simply
    # never read back.
    task_id = models.BigIntegerField()
    day = models.DateField()
    mine = models.PositiveIntegerField(default=0)
    others = models.PositiveIntegerField(default=0)
    notes = models.PositiveIntegerField(default=0)
    chat = models.PositiveIntegerField(default=0)
    prs = models.PositiveIntegerField(default=0)
    # True when a source hit its read cap while this day was built: the counts are a floor.
    capped = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "team", "task_id", "day"], name="uniq_member_item_day"
            )
        ]
        indexes = [
            # "This member's days since", and the retention sweep.
            models.Index(fields=["user", "team", "-day"], name="memberitemday_user_team_day"),
        ]

    def __str__(self) -> str:
        return f"task {self.task_id} on {self.day} ({self.user_id})"


class MemberItemDayState(models.Model):
    state_id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
        related_name="item_activity_states",
    )
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
        related_name="+",
    )
    # The last local day that has been built for this member and team.
    built_through = models.DateField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "team"], name="uniq_member_item_day_state")
        ]


def purge_for_user(user_id) -> int:
    """Delete every row and the watermark of this member. Light on purpose: the opt-out PATCH
    and the account deletion call it without loading the services."""
    deleted, _ = MemberItemDay.objects.filter(user_id=user_id).delete()
    MemberItemDayState.objects.filter(user_id=user_id).delete()
    return deleted
