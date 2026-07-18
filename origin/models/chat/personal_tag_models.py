"""Personal (per-user, PRIVATE) tags on GM channels.

Gmail-label model: a user organizes their own 100+ GM sidebar with
name+color tags that no other member ever sees. Because the data is
per-user it must NEVER ride `ChannelSerializer` — the sockets proxy
broadcasts that serializer to whole channel rooms and the frontend
full-replaces channels from it, so per-user fields there would leak one
user's tags into (and clobber) every member's client state. Tags are
served exclusively by the `/api/v3/personal-tags/` views.

Tags are deliberately USER-scoped, not (team, user)-scoped (unlike
`ToDoCategory`): the sidebar chat list spans teams, so a per-team
namespace would fragment the one chip row the tags exist to power.
"""

from django.db import models

from origin.models.chat.unified_models import Channel
from origin.models.common.user_models import CustomUser


class PersonalChannelTag(models.Model):
    tag_id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
        related_name="personal_channel_tags",
    )
    name = models.CharField(max_length=30)
    # Preset-palette values, mirrors ProjectTags.tag_color/tag_text_color.
    color = models.CharField(max_length=10)
    text_color = models.CharField(max_length=10)
    # "Pinned to the sidebar filter row." When a user has pinned >=1 tag
    # the chip row shows exactly the pinned set; otherwise it falls back
    # to a recency-derived default (tags of GMs they recently sent
    # messages in). Living on the tag row (not a separate preference
    # model) means the preference dies with the tag — no dangling ids.
    is_default_visible = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Case-sensitive at the DB; the view adds an iexact check so
            # "Client" / "client" can't coexist in practice.
            models.UniqueConstraint(fields=["user", "name"], name="uniq_personal_channel_tag"),
        ]


class PersonalChannelTagAssignment(models.Model):
    assignment_id = models.BigAutoField(primary_key=True)
    tag = models.ForeignKey(
        PersonalChannelTag,
        on_delete=models.CASCADE,
        related_name="assignments",
    )
    channel = models.ForeignKey(
        Channel,
        on_delete=models.CASCADE,
        related_name="personal_tag_assignments",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tag", "channel"], name="uniq_personal_tag_assignment"),
        ]
        # No denormalized `user` column: the owner is always `tag.user`,
        # and the all-my-assignments query (`filter(tag__user=...)`) is
        # one indexed FK join at personal scale (<=50 tags x 20 channels).
