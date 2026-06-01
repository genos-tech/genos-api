from django.db import models
from origin.models.common.user_models import CustomUser


class NotificationPreference(models.Model):
    """Per-user web-notification preferences.

    The five boolean toggles are the coarse *group* masters that the
    frontend `NotificationManager` hard-gates on. `category_settings`
    is a free-form `{fine_category_key: bool}` map layered on top of the
    coarse groups so finer sub-categories (e.g. the per-surface mention
    splits) can be added without a schema migration; an absent key
    inherits the category's built-in default. `muted_chats` is a JSON
    list of `{"chat_type": int, "chat_id": str}` entries that suppress
    every category for messages originating from that chat.
    `muted_targets` is a more general per-object mute list — entries of
    `{"target_type", "target_id", "chat_type"?, "categories"?, "label"?}`
    that suppress a specific thread/task/note (optionally only for the
    listed categories).
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

    # Fine-grained per-category overrides layered on the coarse groups.
    # `{fine_key: bool}`; absent key => use the category's default.
    category_settings = models.JSONField(default=dict, blank=True)

    muted_chats = models.JSONField(default=list, blank=True)
    # Per-object mutes (thread/task/note), optionally category-scoped.
    muted_targets = models.JSONField(default=list, blank=True)

    ts_updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"NotificationPreference(user={self.user_id})"
