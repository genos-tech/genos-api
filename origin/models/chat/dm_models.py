from django.db import models
from django.core.exceptions import ValidationError

from origin.models.common.user_models import CustomUser


class DMMaster(models.Model):
    # TODO: add team id for authorization
    dm_id = models.AutoField(primary_key=True)
    user_1_id = models.UUIDField(blank=False, db_index=True)  # should be user_id
    user_2_id = models.UUIDField(blank=False, db_index=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user_1_id", "user_2_id"], name="unique_dm")
        ]

    def clean(self):
        """Custom validation to ensure uniqueness of user pairs."""
        existing_dm = DMMaster.objects.filter(
            models.Q(user_1_id=self.user_1_id, user_2_id=self.user_2_id)
            | models.Q(user_1_id=self.user_2_id, user_2_id=self.user_1_id)
        ).exists()

        if existing_dm:
            raise ValidationError("A DM already exists between these users.")

    def save(self, *args, **kwargs):
        """Ensure validation before saving."""
        self.full_clean()  # Calls the clean method
        super().save(*args, **kwargs)

        # Automatically create mappings for user_1 and user_2
        UserDMMapping.objects.get_or_create(user_id=self.user_1_id, dm_id=self.dm_id)
        UserDMMapping.objects.get_or_create(user_id=self.user_2_id, dm_id=self.dm_id)


class UserDMMapping(models.Model):
    """Maps users to their DM IDs for fast lookup."""

    user_id = models.UUIDField(blank=False, db_index=True)
    dm_id = models.IntegerField(blank=False, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user_id", "dm_id"], name="unique_dm_user_mapping")
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
    message_body = models.TextField(blank=False)
    thread_id = models.IntegerField(blank=True, null=True)
    ts_sent_at = models.DateTimeField(auto_now=True)
    ts_edited_at = models.DateTimeField(null=True, blank=True)
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
    thread_message_body = models.TextField(blank=False)
    parent_message_uid = models.ForeignKey(
        DMMessages,
        on_delete=models.CASCADE,
        related_name="thread_messages",
        to_field="uid",
    )
    ts_sent_at = models.DateTimeField(auto_now=True)
    ts_edited_at = models.DateTimeField(null=True, blank=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    # Refer from Task models
    foreign_thread_id = models.CharField(editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["dm_id", "thread_id", "thread_message_id"], name="unique_dm_thread_message"
            )
        ]

    def save(self, *args, **kwargs):
        self.foreign_thread_id = f"0-{self.dm.dm_id}-{self.thread_id}"
        super().save(*args, **kwargs)
