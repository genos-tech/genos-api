"""Which items a member's AI app touched over MCP (MIP-1143): markers, never text.

A member who works through Claude or ChatGPT never tells Genos anything, and a read over MCP
leaves no trace in the workspace, so the secretary could not see what they were working on.
One row says: this member's connected app OPENED (`read`) or CHANGED (`write`) this item,
around this time. References only: an item type and id, the kind, when, and the app's short
name. No argument text, no result text, no title is ever stored here, and a title shown to
the member later is read live, through the access rules of that moment.

**What goes here and what does not.** What the member did in Genos itself is already saved
in task history, comments, messages, note versions and to-dos (the briefing's `what_you_did`
reads it live). This table holds only what is saved nowhere else: what their AI app looked at.

**Private to the member it describes.** Every read filters on `user`; no teammate, admin or
team dashboard reads it.

**One row per bucket.** At most one row per member, team, item, kind, app and ten-minute
bucket (`bucket` = epoch seconds // 600); another call in the same bucket bumps `ts` and
`count` on that row, so a busy session writes a handful of rows, not one per call per item.
The write is one `INSERT ... ON CONFLICT` (`services/member_events.record`).

**Opt-out, on by default** (`CustomUser.member_events_enabled`). Turning it off deletes
every row of the member in the same transaction as the flag. Kept 180 days
(`member_events.RETENTION`); purged with the account.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser

KIND_READ = "read"
KIND_WRITE = "write"
KIND_CHOICES = ((KIND_READ, "read"), (KIND_WRITE, "write"))

# The short fixed set of things a marker can point at. `note` ids carry their family
# (`personal:12`), because the three note tables number independently.
OBJECT_TASK = "task"
OBJECT_NOTE = "note"
OBJECT_MILESTONE = "milestone"
OBJECT_PROJECT = "project"
OBJECT_CHAT_THREAD = "chat_thread"
OBJECT_TYPES = (OBJECT_TASK, OBJECT_NOTE, OBJECT_MILESTONE, OBJECT_PROJECT, OBJECT_CHAT_THREAD)

SOURCE_MCP = "mcp"
# What `client` holds when the call came on an API key (no app name to give).
CLIENT_API_KEY = "api_key"


class MemberEvent(models.Model):
    event_id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
        related_name="member_events",
    )
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
        related_name="+",
    )
    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    object_type = models.CharField(max_length=24)
    # A string so that a UUID and an integer id both fit.
    object_id = models.CharField(max_length=64)
    source = models.CharField(max_length=16, default=SOURCE_MCP)
    client = models.CharField(max_length=80, default=CLIENT_API_KEY)
    # The last time this marker was touched in its bucket.
    ts = models.DateTimeField(default=timezone.now)
    # epoch seconds // 600: the dedupe key's time part.
    bucket = models.BigIntegerField()
    # How many calls this row stands for.
    count = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "user",
                    "team",
                    "object_type",
                    "object_id",
                    "kind",
                    "client",
                    "source",
                    "bucket",
                ],
                name="uniq_member_event_per_bucket",
            )
        ]
        indexes = [
            # "This member, newest first", and the retention sweep.
            models.Index(fields=["user", "-ts"], name="memberevent_user_ts"),
            # The briefing's read: this member in this team since a time.
            models.Index(fields=["user", "team", "-ts"], name="memberevent_user_team_ts"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.object_type}:{self.object_id} ({self.user_id})"
