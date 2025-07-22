from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.task.task_models import TaskMaster


class DMMaster(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        related_name="team_dm_master",
        to_field="team_id",
    )
    dm_id = models.AutoField(primary_key=True)
    user_1_id = models.UUIDField(blank=False, db_index=True)  # should be user_id
    user_2_id = models.UUIDField(blank=False, db_index=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["team", "user_1_id", "user_2_id"], name="unique_dm")
        ]

    def clean(self):
        """Custom validation to ensure uniqueness of user pairs."""
        existing_dm = DMMaster.objects.filter(
            models.Q(team=self.team, user_1_id=self.user_1_id, user_2_id=self.user_2_id)
            | models.Q(team=self.team, user_1_id=self.user_2_id, user_2_id=self.user_1_id)
        ).exists()

        if existing_dm:
            raise ValidationError("A DM already exists between these users.")

    def save(self, *args, **kwargs):
        """Ensure validation before saving."""
        self.full_clean()  # Calls the clean method
        super().save(*args, **kwargs)

        # Automatically create mappings for user_1 and user_2
        UserDMMapping.objects.get_or_create(
            team_id=self.team.team_id, user_id=self.user_1_id, dm_id=self.dm_id
        )
        UserDMMapping.objects.get_or_create(
            team_id=self.team.team_id, user_id=self.user_2_id, dm_id=self.dm_id
        )


class UserDMMapping(models.Model):
    """Maps users to their DM IDs for fast lookup."""

    team_id = models.UUIDField(blank=False, db_index=True)
    user_id = models.UUIDField(blank=False, db_index=True)
    dm_id = models.IntegerField(blank=False, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team_id", "user_id", "dm_id"], name="unique_dm_user_mapping"
            )
        ]


class DMMessages(models.Model):
    dm = models.ForeignKey(
        DMMaster,
        on_delete=models.CASCADE,
        related_name="dm_messages",
        to_field="dm_id",
    )
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="sent_dm_messages",
        to_field="id",
    )
    receiver = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="received_messages",
        to_field="id",
    )
    message_id = models.IntegerField()
    message_body = models.JSONField(blank=False)
    thread_id = models.IntegerField(blank=True, null=True)
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.CASCADE,
        related_name="dm_thread_task",
        to_field="task_id",
        null=True,
        blank=True,
    )
    ts_sent_at = models.DateTimeField(auto_now_add=True)
    ts_thread_created_at = models.DateTimeField(null=True, blank=True)
    ts_updated_at = models.DateTimeField(auto_now=True)
    uid = models.CharField(primary_key=True, max_length=255, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dm", "message_id"], name="unique_dm_message")
        ]

    def save(self, *args, **kwargs):
        self.uid = f"{self.dm.dm_id}-{self.message_id}"
        super().save(*args, **kwargs)


class DMThreadMessages(models.Model):
    dm = models.ForeignKey(
        DMMaster,
        on_delete=models.CASCADE,
        related_name="dm_thread_messages",
        to_field="dm_id",
    )
    thread_id = models.IntegerField()
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="sent_dm_thread_messages",
        to_field="id",
    )
    receiver = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="received_thread_messages",
        to_field="id",
    )
    thread_message_id = models.IntegerField()
    thread_message_body = models.JSONField(blank=False)
    parent_message_uid = models.ForeignKey(
        DMMessages,
        on_delete=models.CASCADE,
        related_name="thread_messages",
        to_field="uid",
    )
    ts_sent_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["dm_id", "thread_id", "thread_message_id"], name="unique_dm_thread_message"
            )
        ]
