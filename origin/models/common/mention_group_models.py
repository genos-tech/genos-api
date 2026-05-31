from django.db import models
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


# Team-scoped @group mention container (Slack-style). Members are
# carried in `MentionGroupMembers` (one row per user) instead of an M2M
# field so we can audit `added_by` / `ts_created_at` per join and so
# soft-deleting a user doesn't ripple weirdly into the group definition.
class MentionGroupMaster(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="mention_groups",
        to_field="team_id",
    )
    group_id = models.BigAutoField(primary_key=True)
    # Lowercased + dash-friendly name (e.g. "design-team"). Uniqueness is
    # scoped to the team via the constraint below; the @-prefix is added
    # at render time, never stored.
    group_name = models.CharField(max_length=40)
    description = models.CharField(max_length=120, blank=True, default="")
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_mention_groups",
        to_field="id",
    )
    # Soft-delete so messages that still carry a reference to the group
    # in their BlockNote body don't break on read. The resolver returns
    # an empty member set for deleted groups, so live fan-out skips them.
    is_deleted = models.BooleanField(default=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team", "group_name"],
                name="uniq_mention_group_name_per_team",
            )
        ]


class MentionGroupMembers(models.Model):
    # `team` is denormalised so we can scope DELETE-by-team queries
    # without joining through `group` every time. Matches the convention
    # used by `ProjectTags` and friends.
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="mention_group_member_team",
        to_field="team_id",
    )
    group = models.ForeignKey(
        MentionGroupMaster,
        on_delete=models.CASCADE,
        related_name="members",
        to_field="group_id",
    )
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="mention_group_memberships",
        to_field="id",
    )
    added_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "user"],
                name="uniq_user_per_mention_group",
            )
        ]
