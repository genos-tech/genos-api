import uuid

from django.db import models

from origin.models.chat.unified_models import Activity
from origin.models.common.inbox_models import InboxItems
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

    # Independent master for the EMAIL channel — exact sibling of
    # `push_enabled` above. Fine-grained per-category email overrides live
    # in the same `category_settings` map under `email:`-prefixed keys
    # ("email:mention_chat") so no schema change is needed per category;
    # see `services/email_gating.py` for the read side and why email's
    # defaults deliberately differ from push's.
    email_enabled = models.BooleanField(default=True)

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
    # Stable per-browser id the client also sends with its presence
    # heartbeat, so push suppression can tell "the laptop I'm staring at"
    # from "the phone in my pocket". Blank on rows created before
    # per-device presence existed; those fall back to the older
    # any-visible-tab behavior until the client re-registers (which it
    # does on every app load).
    device_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    is_active = models.BooleanField(default=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"PushSubscription(user={self.user_id}, endpoint={self.endpoint[:32]}…)"


class EmailSuppression(models.Model):
    """An address the NOTIFICATION email channel must not send to.

    Written by the Anymail tracking webhook (Resend bounce/complaint
    events — `origin/signals/email_suppression_signals.py`) or manually
    by ops. Deliberately NOT consulted by the transactional mails
    (password reset, verification, invite): those are individually
    requested and must keep working; this list protects the shared
    domain reputation from bulk sends to dead or unwilling addresses.
    """

    REASON_BOUNCE = "bounce"
    REASON_COMPLAINT = "complaint"
    REASON_MANUAL = "manual"

    # Stored lowercased (`email_suppression.suppress` normalizes);
    # unique so re-reports upsert instead of piling up.
    address = models.EmailField(unique=True)
    reason = models.CharField(max_length=16)
    ts_created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"EmailSuppression({self.address}, {self.reason})"


class EmailNotificationEvent(models.Model):
    """Outbox row for the email notification channel.

    Written (post-commit, best-effort) at the same choke points that fan
    out Web Push, then drained by the `email_notify_tick` cron, which
    batches a user's pending rows into ONE email — sent only if the user
    has been away long enough and the underlying items are still unread.
    An outbox (rather than re-deriving from Activity/InboxItems plus a
    high-water mark) is what gives retry semantics and a no-duplicate
    guarantee: rows are claimed by an atomic
    `filter(status=PENDING).update(status=SENDING)`, and a crash mid-pass
    leaves SENDING rows for the stale sweep instead of double-sending.

    `activity` / `inbox_item` exist ONLY so the sender can drop rows whose
    source was read in-app before the email went out. Both are NULL for
    one-off notices (the `schedule_push_to_user` path), which have no
    persisted source row and therefore no read-state to check. SET_NULL,
    not CASCADE: a deleted source just means "can no longer verify
    unread", not "unsend the notification".
    """

    STATUS_PENDING = 0
    STATUS_SENDING = 1
    STATUS_SENT = 2
    STATUS_SKIPPED = 3
    STATUS_FAILED = 4

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="email_notification_events",
    )
    # Fine category key from the shared notification vocabulary
    # (`webpush_gating._PUSH_DEFAULTS` / `email_gating._EMAIL_DEFAULTS`).
    category = models.CharField(max_length=32)
    title = models.CharField(max_length=300)
    body = models.TextField(blank=True, default="")
    # Relative SPA path ("/workspace/..."), same values the push payloads
    # carry; the email renderer prefixes FRONTEND_BASE_URL.
    url = models.CharField(max_length=500, blank=True, default="")
    actor_name = models.CharField(max_length=150, blank=True, default="")
    activity = models.ForeignKey(
        Activity,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    inbox_item = models.ForeignKey(
        InboxItems,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    status = models.PositiveSmallIntegerField(default=STATUS_PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            # The cron's claim query.
            models.Index(fields=["status", "ts_created_at"]),
            # Per-user grouping of pending rows.
            models.Index(fields=["user", "status"]),
            # The per-user cooldown probe (latest sent_at for this user).
            models.Index(fields=["user", "status", "sent_at"]),
        ]

    def __str__(self):
        return (
            f"EmailNotificationEvent(user={self.user_id}, "
            f"category={self.category}, status={self.status})"
        )
