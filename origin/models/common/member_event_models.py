"""The member's ledger (MIP-1143, MIP-1085): references to what happened around their work, never text.

A member who works through Claude or ChatGPT never tells Genos anything, and a read over MCP
leaves no trace in the workspace, so the secretary could not see what they were working on.
One row says: this member's connected app OPENED (`read`) or CHANGED (`write`) this item,
around this time. References only: an item type and id, the kind, when, and the app's short
name. No argument text, no result text, no title is ever stored here, and a title shown to
the member later is read live, through the access rules of that moment.

**What goes here and what does not.** What the member did in Genos itself is already saved
in task history, comments, messages, note versions and to-dos (the briefing's `what_you_did`
reads it live). This table holds only what is saved nowhere else, in two groups of `kind`:

  * `read` / `write` (MIP-1143): what their AI app looked at or changed over MCP;
  * `ACTIVITY_KINDS` (MIP-1085): the member's own changes that nothing saves today. A
    collaborator added or removed, a dependency added or removed, a to-do edited or deleted,
    a note shared or unshared. `source` says how the change reached Genos (`app`, `agent`,
    `api_key`, ...: the same words as a task history row's `via`), `client` is empty, and
    `other_id` holds the other end: the person for a collaborator or a share, the blocking
    task for a dependency (the row's item is the BLOCKED task). The ledger is the only
    place: none of it goes to `TaskActivity`, which the in-app model reads.

**Private to the member it describes.** Every read filters on `user`; no teammate, admin or
team dashboard reads it.

**One row per bucket.** At most one row per member, team, item, kind, other end, app and ten-minute
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
# What the member's own changes are noted as (MIP-1085). Each is one fixed word, so the
# table never holds a sentence.
KIND_COLLABORATOR_ADD = "collaborator_add"
KIND_COLLABORATOR_REMOVE = "collaborator_remove"
KIND_DEPENDENCY_ADD = "dependency_add"
KIND_DEPENDENCY_REMOVE = "dependency_remove"
KIND_TODO_EDIT = "todo_edit"
KIND_TODO_DELETE = "todo_delete"
KIND_NOTE_SHARE = "note_share"
KIND_NOTE_UNSHARE = "note_unshare"
ACTIVITY_KINDS = (
    KIND_COLLABORATOR_ADD,
    KIND_COLLABORATOR_REMOVE,
    KIND_DEPENDENCY_ADD,
    KIND_DEPENDENCY_REMOVE,
    KIND_TODO_EDIT,
    KIND_TODO_DELETE,
    KIND_NOTE_SHARE,
    KIND_NOTE_UNSHARE,
)
# What the AI-app markers are (MIP-1143): the briefing's "looked at" reads only these.
MCP_KINDS = (KIND_READ, KIND_WRITE)
KIND_CHOICES = tuple((k, k) for k in (*MCP_KINDS, *ACTIVITY_KINDS))

# The short fixed set of things a marker can point at. `note` ids carry their family
# (`personal:12`), because the three note tables number independently.
OBJECT_TASK = "task"
OBJECT_NOTE = "note"
OBJECT_MILESTONE = "milestone"
OBJECT_PROJECT = "project"
OBJECT_CHAT_THREAD = "chat_thread"
OBJECT_TODO = "todo"
OBJECT_TYPES = (
    OBJECT_TASK,
    OBJECT_NOTE,
    OBJECT_MILESTONE,
    OBJECT_PROJECT,
    OBJECT_CHAT_THREAD,
    OBJECT_TODO,
)

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
    kind = models.CharField(max_length=24, choices=KIND_CHOICES)
    object_type = models.CharField(max_length=24)
    # A string so that a UUID and an integer id both fit. 128, not 64: a `chat_thread` marker is
    # `chat_id:thread_id`, two UUIDs, 73 characters (it was 64, so none was ever stored).
    object_id = models.CharField(max_length=128)
    # The other end of an activity event (a person's id, or the blocking task's id); empty for
    # the MCP markers. Part of the dedupe key so two people added in one bucket are two rows.
    other_id = models.CharField(max_length=64, default="", db_default="", blank=True)
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
                    "other_id",
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
