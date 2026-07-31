
from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.note.personal_note_models import PersonalNoteFolder


class NotePermissionMaster(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="user_note_permissions",
        to_field="id",
    )
    note_id = models.BigIntegerField(blank=False, null=False)
    # 1: Personal, 2: Task, 3: Chat
    note_type = models.IntegerField(blank=False, null=False)
    # 1: owner, 2: editor, 3: viewer
    role_id = models.IntegerField(blank=False, null=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "note_type", "note_id"], name="unique_note_permission"
            )
        ]


class NoteFolderPermission(models.Model):
    """A role grant on a TEAM folder (`PersonalNoteFolder.scope="team"`).

    Deliberately a SEPARATE table from `NotePermissionMaster` rather than
    an extension of it. That table is keyed
    `UniqueConstraint(user, note_type, note_id)`, so a user can hold
    exactly one role per note. Materializing inherited folder access into
    per-note rows would collide with an explicit per-note grant on that
    constraint and force us to resolve precedence at WRITE time —
    permanently destroying the distinction between "granted this note"
    and "reaches it through the folder". Keeping folder grants here lets
    `get_folder_role` resolve the two at READ time instead.

    Absence of rows on a folder is meaningful: combined with
    `visibility=NULL` it means "inherit from the nearest ancestor that
    has an opinion". Adding a row is how a subfolder NARROWS access
    relative to its parent.

    `via_group_*` records that a grant came from expanding a group
    (mention-group / project / GM) rather than from picking an
    individual. Group membership is SNAPSHOT at invite time — the same
    contract as `MessageMention.via_group_id` — so later joiners do not
    silently gain access behind the search index's back. The provenance
    is what lets the UI say "invited via @eng-team" and offer a
    deliberate re-sync.
    """

    VIA_MENTION_GROUP = "mention_group"
    VIA_PROJECT = "project"
    VIA_GM = "gm"

    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    folder = models.ForeignKey(
        PersonalNoteFolder,
        on_delete=models.CASCADE,
        to_field="folder_id",
        related_name="permissions",
    )
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="user_note_folder_permissions",
        to_field="id",
    )
    # 1: owner, 2: editor, 3: viewer — same vocabulary as
    # NotePermissionMaster.role_id (see views/utils/note_role.py).
    role_id = models.IntegerField(blank=False, null=False)
    via_group_type = models.CharField(max_length=16, blank=True, null=True)
    # Deliberately a CharField: the three group kinds have different id
    # types (mention group int, project int, channel UUID).
    via_group_id = models.CharField(max_length=64, blank=True, null=True)
    granted_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="granted_note_folder_permissions",
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["folder", "user"], name="uniq_note_folder_permission"
            )
        ]


class NoteFolderTag(models.Model):
    """Team-scoped tag vocabulary for organizing many team folders."""

    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    tag_id = models.BigAutoField(primary_key=True, unique=True)
    name = models.CharField(max_length=40)
    # Optional swatch (e.g. "#f97316"); null renders the neutral chip.
    color = models.CharField(max_length=16, blank=True, null=True)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["team", "name"], name="uniq_note_folder_tag_per_team")
        ]


class NoteFolderTagLink(models.Model):
    """Many-to-many join between a team folder and its tags."""

    folder = models.ForeignKey(
        PersonalNoteFolder,
        on_delete=models.CASCADE,
        to_field="folder_id",
        related_name="tag_links",
    )
    tag = models.ForeignKey(
        NoteFolderTag,
        on_delete=models.CASCADE,
        to_field="tag_id",
        related_name="folder_links",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["folder", "tag"], name="uniq_note_folder_tag_link")
        ]
