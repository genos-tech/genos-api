"""The member's worklog (MIP-1071): what they jotted down, and what it means.

A `WorklogEntry` is the raw note, kept exactly as written. A `WorklogItem` is one
thing in it that the member (or a model working for them) pulled out: something
done, a next step, something waited on, a decision, or a plain note, with the words
of the note that show it ("evidence") and links to the tasks and notes it is about.

**Private to its author.** Nothing here is team content: every read and write goes
through `author`, no teammate, admin or team view reaches it, and a row is gone when
the account is deleted (`account_deletion`) and included when it is exported
(`account_export`). `team` is there because the same person can belong to several
teams and works in one at a time; it scopes where the note appears, never who sees it.

`WorklogItem` repeats `author` and `team` from its entry so "my open waiting items"
is one indexed query and so no item can be reached without its owner being checked.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser

KIND_DONE = "done"
KIND_NEXT = "next"
KIND_WAITING = "waiting"
KIND_DECISION = "decision"
KIND_NOTE = "note"
KIND_CHOICES = [
    (KIND_DONE, "done"),
    (KIND_NEXT, "next"),
    (KIND_WAITING, "waiting"),
    (KIND_DECISION, "decision"),
    (KIND_NOTE, "note"),
]

# An item a model suggested starts as `suggested`; one the member confirmed in the
# conversation is `accepted`; one they threw out is `rejected` and stays out of
# lists unless asked for, so a rejected guess is not re-offered.
REVIEW_SUGGESTED = "suggested"
REVIEW_ACCEPTED = "accepted"
REVIEW_REJECTED = "rejected"
REVIEW_CHOICES = [
    (REVIEW_SUGGESTED, "suggested"),
    (REVIEW_ACCEPTED, "accepted"),
    (REVIEW_REJECTED, "rejected"),
]

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_CHOICES = [(STATUS_OPEN, "open"), (STATUS_RESOLVED, "resolved")]

SOURCE_APP = "app"
SOURCE_MCP = "mcp"
SOURCE_AGENT = "agent"
SOURCE_CHOICES = [(SOURCE_APP, "app"), (SOURCE_MCP, "mcp"), (SOURCE_AGENT, "agent")]


class WorklogEntry(models.Model):
    entry_id = models.BigAutoField(primary_key=True)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
        related_name="worklog_entries",
    )
    author = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
        related_name="worklog_entries",
    )
    # What the member wrote, as written. Never rewritten by a model.
    raw_text = models.TextField()
    # When it happened, which is not always when it was jotted ("yesterday I ...").
    occurred_at = models.DateTimeField(default=timezone.now)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_MCP)
    # The client that wrote it ("Claude"), blank until a connection names itself.
    client_name = models.CharField(max_length=100, blank=True, default="")
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["author", "team", "-occurred_at"], name="worklog_entry_author_time_idx"
            ),
        ]


class WorklogItem(models.Model):
    item_id = models.BigAutoField(primary_key=True)
    entry = models.ForeignKey(WorklogEntry, on_delete=models.CASCADE, related_name="items")
    # Repeated from the entry; see the module docstring.
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
        related_name="worklog_items",
    )
    author = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
        related_name="worklog_items",
    )
    position = models.PositiveSmallIntegerField(default=0)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    summary = models.CharField(max_length=280)
    # The words of the entry that show this item, and whether they really are in it.
    evidence_text = models.CharField(max_length=280, blank=True, default="")
    evidence_matched = models.BooleanField(default=False)
    review_state = models.CharField(max_length=16, choices=REVIEW_CHOICES, default=REVIEW_SUGGESTED)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    resolved_at = models.DateTimeField(null=True, blank=True)
    # What the note said about when and who, as words: "Friday", "Aki". Not parsed.
    due_hint = models.CharField(max_length=64, blank=True, default="")
    person_hint = models.CharField(max_length=100, blank=True, default="")
    # [{type, id, display}] for the tasks, notes, milestones and projects it is about,
    # each checked against what the author could open when it was linked.
    refs = models.JSONField(default=list, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["entry_id", "position", "item_id"]
        indexes = [
            models.Index(fields=["author", "team", "status", "kind"], name="worklog_item_open_idx"),
        ]
