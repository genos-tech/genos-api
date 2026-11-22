"""Who changed a project, a sprint, a project label or a tag, and how it arrived (MIP-1758).

Until this table, a change to a project's name or code, a sprint's window or status, a label or a
tag left no row anywhere that named the person (the project and sprint rows keep no actor). One
row says: this person (`actor`) did this (`action`) to this thing (`entity_kind`, `entity_id`),
through this source (`source_kind`, and `source_app` for a connected app's official family).

**What is never stored.** No title, no name, no colour, no description: `fields` holds the NAMES
of the fields that changed (`["project_name"]`, `["tag_name", "tag_color"]`), never their values.
The source is reduced to its kind and, for a connected app's official family only, the family
(`claude`, `chatgpt`). A key's name, a connection id and a token are never written here, so there
is nothing owner-only in the row to hide from a colleague.

**Not logged, on purpose:** the labels a new team is seeded with (`team_setup`): the founding
person did not choose them one by one, and they are the team's starting catalogue.

**Written by one funnel**, `origin.services.workspace_changes.record`, in the same transaction as
the change it describes: a change that committed has its row, and a change that did not has none.

**Kept 365 days**, through `retention_purge` (`approved_days=365`; no default). The actor is an
`ON DELETE SET NULL` link: a soft-deleted account keeps its row (the user row is anonymised, so it
reads "Deleted user"), a hard delete leaves NULL. Rows are never removed by account deletion.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser

ENTITY_PROJECT = "project"
ENTITY_SPRINT = "sprint"
ENTITY_LABEL = "label"
ENTITY_TAG = "tag"
# A group chat (MIP-1959). Its id is a UUID, so its row carries `entity_uuid` and no `entity_id`.
ENTITY_CHANNEL = "channel"
ENTITY_KINDS = (ENTITY_PROJECT, ENTITY_SPRINT, ENTITY_LABEL, ENTITY_TAG, ENTITY_CHANNEL)

ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_DELETE = "delete"
ACTIONS = (ACTION_CREATE, ACTION_UPDATE, ACTION_DELETE)

# The source kinds `get_current_source()` reports that are stored; anything else is stored as "".
# `agent` = the in-app assistant after an approval; `api_key` / `oauth` = MCP or the public API;
# `app` = the signed-in web app (also an Undo click, which carries no more than that).
SOURCE_KINDS = ("agent", "api_key", "oauth", "app")

# A row names at most this many changed fields, each cut to this length (names, never values).
MAX_FIELDS = 8
MAX_FIELD_NAME = 40


class WorkspaceChange(models.Model):
    change_id = models.BigAutoField(primary_key=True)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
        related_name="+",
    )
    # Plain integer, no FK: a project's row can be deleted and its history of names stays readable
    # by its id. A label or a tag of the team's catalogue has no project (null for a label).
    project_id = models.BigIntegerField(null=True, blank=True)
    entity_kind = models.CharField(max_length=16, choices=[(k, k) for k in ENTITY_KINDS])
    # NULL for a channel row (its id is `entity_uuid`); every other kind always has it.
    entity_id = models.BigIntegerField(null=True, blank=True)
    entity_uuid = models.UUIDField(null=True, blank=True)
    action = models.CharField(max_length=16, choices=[(a, a) for a in ACTIONS])
    actor = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workspace_changes",
        to_field="id",
    )
    source_kind = models.CharField(max_length=16, blank=True, default="")
    source_app = models.CharField(max_length=32, blank=True, default="")
    fields = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(
                fields=["team", "entity_kind", "entity_id", "created_at"],
                name="workchg_entity_idx",
            ),
            models.Index(
                fields=["team", "entity_kind", "entity_uuid", "created_at"],
                name="workchg_entity_uuid_idx",
            ),
            models.Index(fields=["created_at"], name="workchg_created_idx"),
        ]
