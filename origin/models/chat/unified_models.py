"""
Unified messaging schema (Phase 0 — shadow tables).

Replaces the four parallel chat-type implementations (DM/GM/PM/MDM) with a
single polymorphic Channel + Message schema. Created empty in Phase 0; the
dual-write helper in Phase 1 will start populating these alongside the
legacy *Messages tables. Backfill in Phase 2. Reads flip in Phase 3.

The natural key (channel_id, seq) is preserved as a UNIQUE constraint so
that existing ReactionFact / ReadStatus / ActivityFact / MentionFact rows
— which reference messages by (chat_type, chat_id, message_id) — keep
working without a cascading FK rewrite. This is what makes the phased
migration safe (see plan: thanks-btw-i-d-like-purrfect-squirrel.md).

PM-specific fields (taskId, displayId, taskStatus, taskCommentCount) live
in `Message.metadata` JSON, not on top-level columns. The PM "one bubble
per task" UI semantic is rendered via a frontend selector (`groupByTask`),
not via a server-side keying scheme — this is what makes the recent PM
duplication bug structurally impossible to recur.
"""

import uuid

from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster


class ChannelKind(models.IntegerChoices):
    DM = 1, "dm"
    GM = 2, "gm"
    PM = 3, "pm"
    MDM = 4, "mdm"


def _channel_profile_image_path(instance, filename):
    return f"channel_profiles/{instance.id}/{filename}"


class Channel(models.Model):
    """Polymorphic chat container. One row per DM/GM/PM/MDM.

    For PM (kind=3) the `project` FK links to ProjectMaster — the legacy
    "project IS the chat" model. For DM/GM/MDM, `project` is null and the
    channel is its own container. DM uniqueness (the unordered user pair)
    is enforced via the ChannelDirectPair side-table; we can't express
    "unordered pair unique" with a partial UniqueConstraint alone.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.PROTECT,
        related_name="channels",
        to_field="team_id",
    )
    kind = models.PositiveSmallIntegerField(choices=ChannelKind.choices)

    # Display
    title = models.CharField(max_length=255, blank=True, default="")
    # `profile_image_url` is the public URL fragment surfaced through
    # the ChannelSerializer + the v3 `channel.update` payload's
    # `profile_image_url` field. `profile_image_file` is the underlying
    # binary upload target — `ChannelProfileImageView` writes the file
    # via FileField and then sets `profile_image_url` to the resolved
    # `"channel_profiles/<id>/<file>"` path so callers don't need two
    # round-trips to render.
    profile_image_url = models.CharField(max_length=512, blank=True, default="")
    profile_image_file = models.FileField(
        upload_to=_channel_profile_image_path,
        blank=True,
        null=True,
    )

    # PM-only: 1:1 with ProjectMaster.
    project = models.ForeignKey(
        ProjectMaster,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="channels",
        to_field="project_id",
    )

    owner = models.ForeignKey(
        CustomUser,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owned_channels",
        to_field="id",
    )
    is_private = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    # Legacy bridge: integer PK of the originating per-type chat row
    # (DMMaster.dm_id, GMMaster.gm_id, ProjectMaster.project_id,
    # MDMMaster.mdm_id). Null for channels created natively by the v3
    # API. The Phase 1 dual-write helper writes this on every legacy
    # POST so the reverse lookup (legacy chat_id → Channel.id) is a
    # single indexed read instead of a fragile heuristic match. Dropped
    # in Phase 7 alongside the legacy tables.
    legacy_chat_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            # PM channels are 1:1 with a project. Partial unique: only
            # applies when kind=PM. Other kinds may have project=null.
            models.UniqueConstraint(
                fields=["project"],
                condition=models.Q(kind=ChannelKind.PM),
                name="uniq_pm_channel_per_project",
            ),
            # (kind, legacy_chat_id) is 1:1 within the legacy schema:
            # one DMMaster row per (team, user pair), one GM/MDMMaster
            # per id, one ProjectMaster per id. Partial: only enforced
            # when legacy_chat_id is set, so v3-native channels (where
            # it stays null) don't collide.
            models.UniqueConstraint(
                fields=["kind", "legacy_chat_id"],
                condition=models.Q(legacy_chat_id__isnull=False),
                name="uniq_channel_legacy_chat_id",
            ),
        ]
        indexes = [
            models.Index(fields=["team", "kind", "is_deleted"], name="channel_team_kind_idx"),
            models.Index(fields=["project"], name="channel_project_idx"),
        ]


class ChannelDirectPair(models.Model):
    """Enforces DM channel uniqueness by unordered user pair.

    Inserted in a signal whenever a kind=DM Channel is created. The pair
    is canonicalized (user_lo, user_hi) so order doesn't matter. Reads
    use this side-table to answer "find the DM between users A and B".
    """

    channel = models.OneToOneField(
        Channel, on_delete=models.CASCADE, primary_key=True, related_name="direct_pair"
    )
    user_lo = models.UUIDField()
    user_hi = models.UUIDField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user_lo", "user_hi"], name="uniq_dm_pair"),
        ]
        indexes = [
            models.Index(fields=["user_lo"], name="dm_pair_lo_idx"),
            models.Index(fields=["user_hi"], name="dm_pair_hi_idx"),
        ]


class ChannelMember(models.Model):
    """One row per (channel, user). Subsumes GMMembers, MDMMembers,
    ProjectMembers-for-PM, and UserDMMapping.

    Phase 1 dual-write keeps the legacy tables in sync via signals;
    after Phase 6 cutover this becomes the sole source of truth.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="channel_memberships",
        to_field="id",
    )
    role = models.CharField(max_length=16, default="member")  # owner | admin | member | system
    is_deleted = models.BooleanField(default=False)
    ts_joined_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["channel", "user"], name="uniq_channel_member"),
        ]
        indexes = [
            models.Index(fields=["user", "channel"], name="member_user_channel_idx"),
            models.Index(fields=["channel", "is_deleted"], name="member_channel_active_idx"),
        ]


class Message(models.Model):
    """One message in a channel. Replaces DMMessages/GMMessages/PMMessages/
    MDMMessages AND their thread-message siblings (threading is just a
    parent FK + is_thread_reply flag on this same table).

    Two identifiers:
      - `id` (UUID): server-issued, the only stable client-facing id.
      - `seq` (int): the legacy per-channel `message_id`. Preserved so
        existing ReactionFact/ReadStatus/ActivityFact/MentionFact rows
        — which reference messages by (chat_type, chat_id, message_id)
        composites — keep resolving without a cascading FK rewrite.

    Frontend IDB stores 1 row per Message keyed by `id`. PM's "one bubble
    per task" UI semantic is a render-time selector (`groupByTask`) that
    collapses N message rows with same `metadata.taskId` into a single
    bubble — it is NOT a storage-layer concern. This is what structurally
    eliminates the recent PM duplication bug.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    channel = models.ForeignKey(Channel, on_delete=models.PROTECT, related_name="messages")
    sender = models.ForeignKey(
        CustomUser,
        null=True,
        on_delete=models.SET_NULL,
        related_name="sent_messages",
        to_field="id",
    )

    # Monotonic per channel. Server allocates on insert. Legacy callers
    # that joined by (chat_type, chat_id, message_id) read this column.
    seq = models.BigIntegerField()

    body = models.JSONField()
    body_text = models.TextField(blank=True, default="")

    # PM-only FK (mirrors PMMessages.task). Other kinds leave null.
    task = models.ForeignKey(
        TaskMaster,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="messages",
        to_field="task_id",
    )

    # Threading: a thread is just a forest of Message rows rooted at one
    # top-level message. `parent` is the immediate parent; `thread_root`
    # is the top-level message of the thread (== parent for 1-deep threads;
    # both null for top-level non-thread messages).
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="direct_replies",
    )
    # CASCADE because the `thread_reply_has_root` CHECK constraint below
    # enforces that any thread reply has a non-null root — so SET_NULL
    # would violate the constraint when the root is hard-deleted. The
    # semantic is also right: a reply with no thread root is meaningless,
    # so the whole thread goes when the root goes. Soft-delete (deleted_at
    # = now()) does not trigger cascade — replies stay visible as
    # tombstones in the parent's thread context.
    thread_root = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="thread_descendants",
    )
    is_thread_reply = models.BooleanField(default=False)

    # Per-kind extra fields go here. PM stores
    # {"taskId": ..., "displayId": "PRJ-12", "taskStatus": "open", "taskCommentCount": 7}.
    # Indexed access uses the `task` FK above; metadata is opaque to SQL.
    metadata = models.JSONField(default=dict, blank=True)

    # Idempotency key for socket `message.send`. The Flask `/v3` layer
    # echoes the client's `correlation_id`; persisting it lets a reconnect
    # flush — which re-emits the SAME correlation_id after a lost/slow ack
    # (the server does not auto-dedup otherwise) — collapse to the
    # already-created row instead of inserting a duplicate Message that
    # everyone in the channel would see twice. Nullable: REST callers and
    # the dual-write mirror don't carry one and are exempt from the
    # partial unique constraint below.
    correlation_id = models.CharField(max_length=64, null=True, blank=True)

    # Denormalized: incremented by a signal on Message.create for thread
    # replies. Read in the chat list to render the reply-count chip
    # without an aggregate query per row.
    reply_count = models.IntegerField(default=0)

    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    ts_sent_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Per-channel monotonic id. Catches double-writes under race.
            models.UniqueConstraint(fields=["channel", "seq"], name="uniq_channel_seq"),
            # Per-channel idempotency on the socket correlation_id. A
            # reconnect flush re-emits `message.send` with the same id; the
            # create path returns the existing row instead of duplicating.
            # Partial (correlation_id IS NOT NULL) so REST / dual-write rows
            # without a correlation_id aren't forced unique.
            models.UniqueConstraint(
                fields=["channel", "correlation_id"],
                condition=models.Q(correlation_id__isnull=False),
                name="uniq_channel_correlation",
            ),
            # A thread reply must point at a root.
            models.CheckConstraint(
                check=(models.Q(is_thread_reply=False) | models.Q(thread_root__isnull=False)),
                name="thread_reply_has_root",
            ),
        ]
        indexes = [
            # Scroll / pagination by time.
            models.Index(fields=["channel", "ts_sent_at"], name="msg_channel_ts_idx"),
            # Thread expansion.
            models.Index(fields=["thread_root", "ts_sent_at"], name="msg_thread_ts_idx"),
            # Delta sync ("?since=...").
            models.Index(fields=["channel", "ts_updated_at"], name="msg_channel_updated_idx"),
            # PM: lookup by (channel, task) for the groupByTask selector.
            models.Index(fields=["channel", "task"], name="msg_channel_task_idx"),
            # Legacy join surface: (chat_type, chat_id, message_id) →
            # (channel.kind, channel_id, seq). The (channel, seq) unique
            # index above already covers the seq half; add an index on
            # the underlying chat_type+chat_id via the channel FK +
            # the channel.kind index. No separate index needed here.
        ]


class MessageReaction(models.Model):
    """Reactions on the unified Message table. Will eventually replace
    ReactionFact (which is keyed by composite chat_type/chat_id/message_id).
    During Phase 1–6 dual-write, ReactionFact stays authoritative; the new
    table is shadow-written so the unified delta serializer has data."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="message_reactions",
        to_field="id",
    )
    emoji = models.CharField(max_length=64)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["message", "user", "emoji"], name="uniq_message_reaction"
            ),
        ]
        indexes = [
            models.Index(fields=["message"], name="reaction_message_idx"),
            models.Index(fields=["message", "ts_updated_at"], name="reaction_msg_updated_idx"),
        ]


class MessageMention(models.Model):
    """Mentions on the unified Message table. Will eventually replace
    MentionFact. Carries optional via_group reference for group mentions
    (replaces the mentioned_via_groups JSON in ActivityFact)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="mentions")
    mentioned_user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="message_mentions",
        to_field="id",
    )
    # Null = direct @user mention. Non-null = mention via group @everyone /
    # @engineering / etc. Lets the inbox surface "@you via @engineering".
    # Group resolution happens at mention-write time (the user list is
    # snapshotted) so changes to group membership don't retroactively
    # rewrite history.
    via_group_id = models.UUIDField(null=True, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["message", "mentioned_user"], name="uniq_message_mention"
            ),
        ]
        indexes = [
            # Inbox: "all mentions of me, newest first".
            models.Index(fields=["mentioned_user", "ts_created_at"], name="mention_user_ts_idx"),
            models.Index(fields=["message"], name="mention_message_idx"),
        ]


def _message_attachment_path(instance, filename):
    return f"chats/{instance.message.channel_id}/messages/{instance.message_id}/{filename}"


class MessageAttachment(models.Model):
    """One row per file uploaded with a message. Replaces the universal
    ChatAttachmentFact AND the 4 per-type AttachmentFact tables — those
    duplicated columns and only one was ever read."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="attachments")
    uploader = models.ForeignKey(
        CustomUser,
        null=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_attachments",
        to_field="id",
    )
    # Default `max_length=100` is too short for our path layout —
    # `chats/<channel-uuid>/messages/<message-uuid>/<filename>` is ~89
    # chars BEFORE the filename, leaving 11 chars for filename + the
    # random uniqueness suffix Django appends on collisions, which
    # blows up as `SuspiciousFileOperation`. 500 leaves room for long
    # filenames (e.g. user-supplied document names) + suffixes.
    file = models.FileField(upload_to=_message_attachment_path, max_length=500)
    mime = models.CharField(max_length=128, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["message"], name="attachment_message_idx"),
        ]


class ReadCursor(models.Model):
    """Per-user, per-channel (and per-thread) read pointer. One cursor per
    (user, channel) for the main timeline; additional cursors per
    (user, channel, thread_root) for each thread the user has opened.

    Replaces ReadStatus, which keyed cursors by (user, chat_type, chat_id,
    thread_id) composite. The new shape uses real FKs so the rows are
    join-friendly and don't drift when channels/messages get deleted.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="read_cursors",
        to_field="id",
    )
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="read_cursors")
    # Null = main timeline cursor. Non-null = per-thread cursor pointing
    # at the thread's root Message.
    thread_root = models.ForeignKey(
        Message,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="thread_read_cursors",
    )
    last_read_message = models.ForeignKey(
        Message,
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    last_read_at = models.DateTimeField(auto_now=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # NULL-safe uniqueness: one main-timeline cursor per (user, channel),
            # one per-thread cursor per (user, channel, thread_root).
            models.UniqueConstraint(
                fields=["user", "channel"],
                condition=models.Q(thread_root__isnull=True),
                name="uniq_main_cursor",
            ),
            models.UniqueConstraint(
                fields=["user", "channel", "thread_root"],
                condition=models.Q(thread_root__isnull=False),
                name="uniq_thread_cursor",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "channel"], name="cursor_user_channel_idx"),
        ]


class Pin(models.Model):
    """Pinned channels per user. Replaces UserChatMaster.pinned_chats JSON,
    which stored a list of {chat_type, chat_id} dicts and required parsing
    to filter / sort."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="pinned_channels",
        to_field="id",
    )
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="pins")
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "channel"], name="uniq_pin"),
        ]
        indexes = [
            models.Index(fields=["user", "ts_created_at"], name="pin_user_ts_idx"),
        ]


class Flag(models.Model):
    """Flagged messages per user. Replaces UserChatMaster.flagged_messages
    JSON, which stored a list of {chat_type, chat_id, thread_id, message_id}
    dicts. The new shape uses a real FK to Message so deletions cascade
    cleanly and the inbox can join."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="flagged_messages",
        to_field="id",
    )
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="flags")
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "message"], name="uniq_flag"),
        ]
        indexes = [
            models.Index(fields=["user", "ts_created_at"], name="flag_user_ts_idx"),
        ]


class ActivityType(models.IntegerChoices):
    """Discriminator for `Activity.activity_type`. Mirrors the legacy
    sidebar's filter chips so the UI can keep the same surface."""

    THREAD_REPLY = 1, "thread_reply"
    REACTION = 2, "reaction"
    MENTION = 3, "mention"
    TASK_ASSIGN = 4, "task_assign"


class Activity(models.Model):
    """One activity-feed entry per (recipient, triggering message).

    Replaces the legacy `ActivityFact` table. Where the legacy schema
    used integer `chat_type` / `chat_id` / `message_id` composites,
    this version FKs into the unified `Channel` and `Message` tables.

    `recipient` is the user this entry is FOR — they're the one whose
    sidebar shows it. `actor` is who performed the action (reacted /
    replied / mentioned). The two are almost always different; the
    one exception is self-reactions, which we skip at the producer
    level so they never reach the table.

    `meta` carries activity-type-specific payload — e.g. reactions
    store `{"emoji": "👍"}`; mentions carry `{"via_groups": [...]}`
    when the user was reached via group expansion. Read at render
    time only; never indexed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        related_name="activities",
        to_field="team_id",
    )
    recipient = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="received_activities",
        to_field="id",
    )
    actor = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="emitted_activities",
        to_field="id",
    )
    activity_type = models.PositiveSmallIntegerField(choices=ActivityType.choices)
    # `channel` / `message` are null for "surface" activities that aren't
    # backed by a chat message — task-body and note @-mentions. Those carry
    # `surface_type` (the legacy chat_type namespace: 5=task body,
    # 6=personal note, 7=task note, 8=chat note) and stash their routing
    # ids (task_id / project_id / note_id / chat_id …) in `meta`. Channel-
    # backed activities (mention / reaction / thread-reply on a real
    # Message) leave `surface_type` null and the FE derives chat_type from
    # `channel.kind` (1-4) as before.
    channel = models.ForeignKey(
        Channel, on_delete=models.CASCADE, related_name="activities", null=True, blank=True
    )
    message = models.ForeignKey(
        Message, on_delete=models.CASCADE, related_name="activities", null=True, blank=True
    )
    surface_type = models.PositiveSmallIntegerField(null=True, blank=True)
    meta = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            # The hot read path: the recipient's sidebar, newest-first.
            models.Index(fields=["recipient", "-ts_created_at"], name="activity_recipient_ts_idx"),
            # Unread-only filter is the second hot read.
            models.Index(
                fields=["recipient", "is_read", "-ts_created_at"],
                name="activity_recipient_unread_idx",
            ),
        ]
