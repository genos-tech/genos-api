import uuid

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

    # Independent master for OS/Web Push (vs. the in-app toasts/Notification
    # path, which `master_enabled` governs). Lets a user keep in-app
    # notifications while turning off away-from-app push, or vice-versa.
    # The per-category / coarse-group / mute rules still apply on top.
    push_enabled = models.BooleanField(default=True)

    # Fine-grained per-category overrides layered on the coarse groups.
    # `{fine_key: bool}`; absent key => use the category's default.
    category_settings = models.JSONField(default=dict, blank=True)

    muted_chats = models.JSONField(default=list, blank=True)
    # Per-object mutes (thread/task/note), optionally category-scoped.
    muted_targets = models.JSONField(default=list, blank=True)

    ts_updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"NotificationPreference(user={self.user_id})"


class PushSubscription(models.Model):
    """A browser Web Push subscription (one row per browser/device).

    Created when a user grants notification permission and the service
    worker subscribes via `pushManager.subscribe(...)`. The server sends
    Web Push messages to `endpoint` (signed with the server's VAPID key),
    encrypted with the `p256dh` / `auth` keys the browser generated. A
    user can have several (one per browser/device); `endpoint` is globally
    unique. Rows are deleted when the push service reports the endpoint
    gone (HTTP 404/410) — see `webpush_sender`.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="push_subscriptions",
    )

    # Push service URL — can be long (FCM/Mozilla); TextField avoids a
    # length cap. Globally unique: the same browser re-subscribing upserts.
    endpoint = models.TextField(unique=True)
    # Browser-generated encryption material (base64url).
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)

    user_agent = models.CharField(max_length=500, blank=True, default="")
    is_active = models.BooleanField(default=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"PushSubscription(user={self.user_id}, endpoint={self.endpoint[:32]}…)"
