from django.db import models
from origin.models.common.user_models import CustomUser


class NotificationPreference(models.Model):
    """Per-user web-notification preferences.

    Five master toggles map 1:1 to the categories the frontend
    `NotificationManager` knows about. `muted_chats` is a JSON list of
    `{"chat_type": int, "chat_id": str}` entries that suppress every
    category for messages originating from that chat.
    """

    user = models.OneToOneField(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="notification_preference",
    )

    master_enabled = models.BooleanField(default=True)
    enable_chats = models.BooleanField(default=True)
    enable_thread_replies = models.BooleanField(default=True)
    enable_mentions = models.BooleanField(default=True)
    enable_task_comments = models.BooleanField(default=True)
    enable_inbox = models.BooleanField(default=True)

    muted_chats = models.JSONField(default=list, blank=True)

    ts_updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"NotificationPreference(user={self.user_id})"
