"""The agent call log (MIP-2006): one private row per MCP tool call, ids and tool names only.

Design: the note "Findings: agent (MCP) audit trail" section 4.3 and the decision note (D1-D8,
Ken, 2026-11-22). What an AI app CHANGED is already on the item (task activity, note versions,
comments). This table holds what the item cannot: what the app read or searched, calls that were
refused or failed, which run they came from, how long they took.

**No text, ever.** There is no field for an argument, a search query, a result, an IP address or a
user agent, and none may be added: the landing page promises "it does not store the full text of
your conversations", and a search query can hold personal data. `targets` holds only `{type, id}`
pairs (the ids the call named, after references were resolved), `error_code` is a short word the
server chose, and `tool` is a registered tool's name or `unknown`. A test pins that a search query
never reaches the table (D5).

**Private to the person** (D3): every read filters on `user`; no teammate, admin or team owner reads
a row. A team owner will see counts per member per app, never another member's reads.

**Collapsed reads.** Reads of the same object by the same credential within ten minutes are one
row with a `count` (`services/agent_calls.record`), so a model that re-reads a task twenty times
writes one row.

**Switched off by default** (`AGENT_CALL_LOG`): it is switched on in production only after the
privacy-page line (MIP-2007) is merged. Kept 90 days (`services/agent_calls.RETENTION_DAYS`,
daily prune command `prune_agent_calls`); included in the data export; deleted with the account.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser

CREDENTIAL_OAUTH = "oauth"
CREDENTIAL_API_KEY = "api_key"
CREDENTIAL_CHOICES = ((CREDENTIAL_OAUTH, "oauth"), (CREDENTIAL_API_KEY, "api_key"))

APP_CLAUDE = "claude"
APP_CHATGPT = "chatgpt"
APP_OTHER = "other"
APP_CHOICES = ((APP_CLAUDE, "claude"), (APP_CHATGPT, "chatgpt"), (APP_OTHER, "other"))

OP_READ = "read"
OP_SEARCH = "search"
OP_WRITE = "write"
OP_DELETE = "delete"
OP_ADMIN = "admin"
OP_CHOICES = tuple((o, o) for o in (OP_READ, OP_SEARCH, OP_WRITE, OP_DELETE, OP_ADMIN))

OUTCOME_OK = "ok"
OUTCOME_TOOL_ERROR = "tool_error"
OUTCOME_REFUSED_SCOPE = "refused_scope"
OUTCOME_REFUSED_DESTRUCTIVE = "refused_destructive"
OUTCOME_REFUSED_LIMIT = "refused_limit"
OUTCOME_FAILED = "failed"
OUTCOME_CHOICES = tuple(
    (o, o)
    for o in (
        OUTCOME_OK,
        OUTCOME_TOOL_ERROR,
        OUTCOME_REFUSED_SCOPE,
        OUTCOME_REFUSED_DESTRUCTIVE,
        OUTCOME_REFUSED_LIMIT,
        OUTCOME_FAILED,
    )
)

# At most this many `{type, id}` pairs on one row.
MAX_TARGETS = 10


class AgentCall(models.Model):
    id = models.BigAutoField(primary_key=True)
    ts = models.DateTimeField(default=timezone.now)
    # CASCADE for a hard delete; a soft-deleted account is purged by hand in `account_deletion`.
    user = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, to_field="id", related_name="agent_calls"
    )
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
        related_name="+",
        null=True,
        blank=True,
    )
    credential_kind = models.CharField(max_length=16, choices=CREDENTIAL_CHOICES)
    # The connection's or key's id (a UUID), as text so the table has no FK to a revocable row.
    credential_id = models.CharField(max_length=40, blank=True, default="")
    app_family = models.CharField(max_length=16, choices=APP_CHOICES, default=APP_OTHER)
    # A synthetic session: one id per credential until it has been quiet for 20 minutes.
    run_id = models.CharField(max_length=32, blank=True, default="")
    tool = models.CharField(max_length=64)
    op = models.CharField(max_length=8, choices=OP_CHOICES)
    outcome = models.CharField(max_length=24, choices=OUTCOME_CHOICES, default=OUTCOME_OK)
    # A short word the server wrote (never the caller's text); empty for an `ok` call.
    error_code = models.CharField(max_length=64, blank=True, default="")
    # At most `MAX_TARGETS` of {"type": "task", "id": "123"}. Ids only.
    targets = models.JSONField(default=list, blank=True)
    # Items affected, or hits for a search; reads of one object collapse into one row and add up.
    count = models.PositiveIntegerField(default=1)
    duration_ms = models.PositiveIntegerField(default=0)
    result_chars = models.PositiveIntegerField(default=0)
    credits = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)

    class Meta:
        indexes = [
            # "This person, newest first", the export and the 90-day sweep.
            models.Index(fields=["user", "-ts"], name="agentcall_user_ts"),
            # One run's calls.
            models.Index(fields=["run_id"], name="agentcall_run"),
            # The daily prune reads "older than 90 days" across all people.
            models.Index(fields=["ts"], name="agentcall_ts"),
        ]

    def __str__(self) -> str:
        return f"{self.tool} {self.outcome} ({self.user_id})"


def new_run_id() -> str:
    return uuid.uuid4().hex
