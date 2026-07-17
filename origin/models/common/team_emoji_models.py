import uuid

from django.db import models
from django.db.models import Q

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


def _team_emoji_path(instance, filename):
    """Storage path for a custom emoji image.

    The client-supplied filename is deliberately discarded: `name` is
    regex-validated by the view and the extension comes from an
    allowlist, so every path segment is built from trusted parts. The
    uuid prefix keeps re-uploads of a reused name from colliding with
    the soft-deleted predecessor's file (which is kept on disk so
    message bodies that baked its URL keep rendering).
    """
    ext = instance.image_ext or "png"
    scope = instance.team_id or "global"
    return f"team_emoji/{scope}/{uuid.uuid4()}-{instance.name}.{ext}"


class TeamEmojiMaster(models.Model):
    """Slack-style team custom emoji (static or animated).

    Referenced from BlockNote bodies as an inline `customEmoji` node
    whose props bake the name AND the absolute image URL at insert
    time, and from message reactions as the `:name:` shortcode string.
    Soft delete keeps the image file so old content keeps rendering;
    only the catalog (GET) hides deleted rows, which makes new inserts
    and reaction rendering fall back gracefully.

    `team=NULL` rows are GLOBAL DEFAULTS (the seeded starter packs):
    every team's catalog includes them automatically — current and
    future teams alike — and a team emoji with the same name overrides
    the global one. They're managed via `seed_team_emoji --global`,
    never through the API (no uploader ⇒ the DELETE endpoint refuses).
    """

    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="custom_emoji",
        to_field="team_id",
    )
    emoji_id = models.BigAutoField(primary_key=True)
    # Lowercase shortcode without colons (e.g. "party-blob"). Max 50 so
    # the rendered `:name:` (name + 2) always fits the reaction column
    # (`MessageReaction.emoji`, CharField(64)).
    name = models.CharField(max_length=50)
    # Transient carrier for the validated extension so `upload_to` can
    # build the path without re-deriving it. Not a DB field.
    image_ext = ""
    image = models.FileField(upload_to=_team_emoji_path, max_length=500)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_team_emoji",
        to_field="id",
    )
    is_deleted = models.BooleanField(default=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Partial (active-only) on purpose, unlike the mention-group
            # constraint: deleting an emoji must free its name for
            # re-upload (Slack semantics). Old bodies keep the old URL;
            # a re-created name gets a fresh uuid-prefixed file.
            models.UniqueConstraint(
                fields=["team", "name"],
                condition=Q(is_deleted=False),
                name="uniq_active_team_emoji_name",
            ),
            # Postgres treats NULLs as distinct in unique indexes, so
            # the constraint above never dedupes GLOBAL (team=NULL)
            # rows — this one does.
            models.UniqueConstraint(
                fields=["name"],
                condition=Q(team__isnull=True, is_deleted=False),
                name="uniq_active_global_emoji_name",
            ),
        ]
