"""No activity row for a recipient who is looking at the conversation.

Two halves, tested separately because they fail differently:

  **`services/presence` viewing keys** — what the heartbeat writes, and
  the retraction that keeps a stale claim from suppressing activities
  after the user moved on. This is the half where a bug silently *loses*
  a user's feed entries, so the retraction paths get as much coverage as
  the happy path.

  **`services/v3_activity` producers** — which fan-outs consult it. The
  negative cases (viewing a *different* conversation, viewing the channel
  while the reply is in a thread) matter more than the positive one: they
  are what keeps this from turning into "activities stop working while
  the app is open".

`settings_test` swaps Redis for LocMemCache, so the presence keys are
real and deterministic here.
"""

from django.core.cache import cache

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
)
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.services import presence
from origin.services.v3_activity import (
    channel_surface,
    create_comment_participant_activities,
    create_mention_activities,
    create_message_activities,
    create_reaction_activity,
    create_thread_reply_activity,
    message_surface,
    task_surface,
    thread_surface,
)
from origin.tests.test_base import BaseAPITestCase

DEVICE = "device-a"


class PresenceViewingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_marks_and_reads_a_surface(self):
        presence.mark_viewing(self.user.id, "channel:abc", DEVICE)
        self.assertTrue(presence.is_viewing(self.user.id, "channel:abc"))
        self.assertFalse(presence.is_viewing(self.user.id, "channel:other"))
        self.assertFalse(presence.is_viewing(self.user2.id, "channel:abc"))

    def test_moving_to_another_surface_retracts_the_old_one(self):
        """The reason `_viewing_device_key` exists. Without it, walking
        through five chats would leave all five suppressing activities for
        the rest of the TTL."""
        presence.mark_viewing(self.user.id, "channel:first", DEVICE)
        presence.mark_viewing(self.user.id, "channel:second", DEVICE)
        self.assertFalse(presence.is_viewing(self.user.id, "channel:first"))
        self.assertTrue(presence.is_viewing(self.user.id, "channel:second"))

    def test_an_empty_surface_retracts(self):
        """Navigating to a non-conversation page (inbox, settings) sends an
        empty surface rather than omitting the field."""
        presence.mark_viewing(self.user.id, "channel:first", DEVICE)
        presence.mark_viewing(self.user.id, "", DEVICE)
        self.assertFalse(presence.is_viewing(self.user.id, "channel:first"))

    def test_clear_viewing_retracts_the_current_surface(self):
        presence.mark_viewing(self.user.id, "task:7", DEVICE)
        presence.clear_viewing(self.user.id, DEVICE)
        self.assertFalse(presence.is_viewing(self.user.id, "task:7"))

    def test_two_devices_on_different_surfaces_coexist(self):
        presence.mark_viewing(self.user.id, "channel:laptop", "d1")
        presence.mark_viewing(self.user.id, "channel:phone", "d2")
        self.assertTrue(presence.is_viewing(self.user.id, "channel:laptop"))
        self.assertTrue(presence.is_viewing(self.user.id, "channel:phone"))

    def test_a_junk_surface_is_refused_not_stored(self):
        """Surfaces are client-supplied and become cache keys."""
        for junk in ("has space", "new\nline", "x" * 200 + "!"):
            presence.mark_viewing(self.user.id, junk, DEVICE)
            self.assertFalse(presence.is_viewing(self.user.id, junk))

    def test_viewers_of_is_the_subset_that_is_looking(self):
        presence.mark_viewing(self.user.id, "channel:x", DEVICE)
        self.assertEqual(
            presence.viewers_of("channel:x", [str(self.user.id), str(self.user2.id)]),
            {str(self.user.id)},
        )

    def test_viewers_of_handles_empty_inputs(self):
        self.assertEqual(presence.viewers_of("", [str(self.user.id)]), set())
        self.assertEqual(presence.viewers_of("channel:x", []), set())

    def test_the_heartbeat_endpoint_records_the_surface(self):
        """End-to-end through the route the frontend actually calls."""
        self.authenticate(self.user)
        resp = self.client.post(
            "/api/v2/user/presence/heartbeat/",
            {"device_id": DEVICE, "surface": "channel:from-http"},
            format="json",
        )
        self.assertEqual(resp.status_code, 204)
        self.assertTrue(presence.is_viewing(self.user.id, "channel:from-http"))
        # Visible-tab presence still recorded — the surface rides along, it
        # doesn't replace it.
        self.assertTrue(presence.is_device_visible(self.user.id, DEVICE))

        resp = self.client.delete(
            "/api/v2/user/presence/heartbeat/", {"device_id": DEVICE}, format="json"
        )
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(presence.is_viewing(self.user.id, "channel:from-http"))

    def test_a_heartbeat_without_a_surface_is_still_valid(self):
        """Older clients (and the very first beat after load) send only the
        device id."""
        self.authenticate(self.user)
        resp = self.client.post(
            "/api/v2/user/presence/heartbeat/", {"device_id": DEVICE}, format="json"
        )
        self.assertEqual(resp.status_code, 204)
        self.assertTrue(presence.is_device_visible(self.user.id, DEVICE))


class MessageActivitySuppressionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.DM, title="", owner=self.user
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="member")
        ChannelMember.objects.create(channel=self.channel, user=self.user2, role="member")
        self.message = Message.objects.create(
            channel=self.channel, sender=self.user, seq=1, body={"text": "hi"}, body_text="hi"
        )

    def _fan_out(self):
        return create_message_activities(
            message=self.message, recipient_ids=[str(self.user2.id)], actor=self.user
        )

    def test_row_is_written_when_the_recipient_is_not_looking(self):
        self.assertEqual(len(self._fan_out()), 1)

    def test_no_row_for_a_recipient_with_this_dm_open(self):
        presence.mark_viewing(self.user2.id, channel_surface(self.channel.id), DEVICE)
        self.assertEqual(self._fan_out(), [])
        self.assertEqual(Activity.objects.count(), 0)

    def test_a_recipient_reading_a_different_chat_still_gets_the_row(self):
        other = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Other")
        presence.mark_viewing(self.user2.id, channel_surface(other.id), DEVICE)
        self.assertEqual(len(self._fan_out()), 1)

    def _third_member(self):
        from django.contrib.auth import get_user_model

        from origin.models.common.team_models import TeamMembers

        third = get_user_model().objects.create_user(
            username="third", email="third@example.com", password="pw123456"
        )
        TeamMembers.objects.create(team=self.team, attendee=third)
        ChannelMember.objects.create(channel=self.channel, user=third, role="member")
        return third

    def test_only_the_looking_recipient_is_dropped(self):
        """`Activity` is per-recipient, so suppression must be too."""
        third = self._third_member()
        presence.mark_viewing(self.user2.id, channel_surface(self.channel.id), DEVICE)
        rows = create_message_activities(
            message=self.message,
            recipient_ids=[str(self.user2.id), str(third.id)],
            actor=self.user,
        )
        self.assertEqual([str(r.recipient_id) for r in rows], [str(third.id)])

    def test_a_mention_in_the_open_chat_is_suppressed_too(self):
        """Being @-mentioned in a chat you are reading is the same
        non-event as being messaged in it — you watched it arrive."""
        presence.mark_viewing(self.user2.id, channel_surface(self.channel.id), DEVICE)
        rows = create_mention_activities(
            message=self.message, mentioned_user_ids=[str(self.user2.id)], actor=self.user
        )
        self.assertEqual(rows, [])
        self.assertEqual(Activity.objects.count(), 0)

    def test_a_mention_is_written_when_the_recipient_is_reading_elsewhere(self):
        other = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Other")
        presence.mark_viewing(self.user2.id, channel_surface(other.id), DEVICE)
        rows = create_mention_activities(
            message=self.message, mentioned_user_ids=[str(self.user2.id)], actor=self.user
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].activity_type, ActivityType.MENTION)

    def test_only_the_looking_mention_target_is_dropped(self):
        third = self._third_member()
        presence.mark_viewing(self.user2.id, channel_surface(self.channel.id), DEVICE)
        rows = create_mention_activities(
            message=self.message,
            mentioned_user_ids=[str(self.user2.id), str(third.id)],
            actor=self.user,
        )
        self.assertEqual([str(r.recipient_id) for r in rows], [str(third.id)])

    def test_a_bot_mention_is_never_suppressed(self):
        """A task-assignment card @-mentions its assignee from the project
        bot. That is work being handed over, not conversation, and PM
        channels write no plain-message row — so the feed entry is the only
        durable trace and must survive having the chat open."""
        from django.contrib.auth import get_user_model

        bot = get_user_model().objects.create_user(
            username="bot", email="bot@example.com", password="pw123456", is_system_user=True
        )
        card = Message.objects.create(
            channel=self.channel, sender=bot, seq=2, body={"text": "task"}, body_text="task"
        )
        presence.mark_viewing(self.user2.id, channel_surface(self.channel.id), DEVICE)
        rows = create_mention_activities(
            message=card, mentioned_user_ids=[str(self.user2.id)], actor=bot
        )
        self.assertEqual(len(rows), 1)


class ReactionSuppressionTests(BaseAPITestCase):
    """A reaction lands under a message that is already on screen — the
    emoji animates in, so a feed row about it is a second copy."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.DM, title="", owner=self.user
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="member")
        ChannelMember.objects.create(channel=self.channel, user=self.user2, role="member")
        # user2 wrote it, so user2 is who a reaction would be announced to.
        self.message = Message.objects.create(
            channel=self.channel, sender=self.user2, seq=1, body={"text": "hi"}, body_text="hi"
        )

    def _react(self, message=None):
        return create_reaction_activity(
            message=message or self.message, emoji="👍", actor=self.user
        )

    def test_row_is_written_when_the_sender_is_not_looking(self):
        rows = self._react()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].activity_type, ActivityType.REACTION)

    def test_no_row_when_the_sender_has_the_chat_open(self):
        presence.mark_viewing(self.user2.id, channel_surface(self.channel.id), DEVICE)
        self.assertEqual(self._react(), [])
        self.assertEqual(Activity.objects.count(), 0)

    def test_a_sender_reading_a_different_chat_still_gets_the_row(self):
        other = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Other")
        presence.mark_viewing(self.user2.id, channel_surface(other.id), DEVICE)
        self.assertEqual(len(self._react()), 1)

    def test_a_reaction_on_a_thread_reply_follows_the_thread(self):
        root = Message.objects.create(
            channel=self.channel, sender=self.user, seq=2, body={"text": "r"}, body_text="r"
        )
        reply = Message.objects.create(
            channel=self.channel,
            sender=self.user2,
            seq=3,
            body={"text": "re"},
            body_text="re",
            thread_root=root,
            is_thread_reply=True,
        )
        # Reading the timeline behind the pane is not reading the thread.
        presence.mark_viewing(self.user2.id, channel_surface(self.channel.id), DEVICE)
        self.assertEqual(len(self._react(reply)), 1)

        presence.mark_viewing(self.user2.id, thread_surface(root.id), DEVICE)
        self.assertEqual(self._react(reply), [])


class ThreadReplySuppressionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="C", owner=self.user
        )
        self.root = Message.objects.create(
            channel=self.channel, sender=self.user2, seq=1, body={"text": "root"}, body_text="root"
        )
        self.reply = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=2,
            body={"text": "re"},
            body_text="re",
            thread_root=self.root,
            is_thread_reply=True,
        )

    def _fan_out(self):
        return create_thread_reply_activity(reply=self.reply, parent=self.root, actor=self.user)

    def test_no_row_for_a_participant_with_the_thread_open(self):
        presence.mark_viewing(self.user2.id, thread_surface(self.root.id), DEVICE)
        self.assertEqual(self._fan_out(), [])

    def test_the_channel_being_open_does_not_suppress_a_thread_reply(self):
        """Thread replies live behind a pane: someone reading the main
        timeline has NOT seen this, so they must still be told."""
        presence.mark_viewing(self.user2.id, channel_surface(self.channel.id), DEVICE)
        self.assertEqual(len(self._fan_out()), 1)


class TaskCommentSuppressionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        project = ProjectMaster.objects.create(team=self.team, project_name="P", owner=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=project, title="T", status="Open", reporter=self.user
        )
        # A project's PM channel is created for it by `pm_channel_signals`
        # and is unique per project — creating a second one here would trip
        # `uniq_pm_channel_per_project`.
        self.channel = Channel.objects.get(project=project, kind=ChannelKind.PM)
        self.mirror = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body={"text": "c"},
            body_text="c",
            task_id=self.task.task_id,
        )

    def _fan_out(self):
        return create_comment_participant_activities(
            message=self.mirror, recipient_ids=[str(self.user2.id)], actor=self.user
        )

    def test_no_row_for_a_participant_with_the_task_open(self):
        presence.mark_viewing(self.user2.id, task_surface(self.task.task_id), DEVICE)
        self.assertEqual(self._fan_out(), [])

    def test_row_is_written_for_a_participant_looking_elsewhere(self):
        presence.mark_viewing(self.user2.id, task_surface(self.task.task_id + 1), DEVICE)
        self.assertEqual(len(self._fan_out()), 1)

    def test_the_pm_channel_being_open_does_not_suppress_a_comment(self):
        """The comment renders in the task preview, not the project chat."""
        presence.mark_viewing(self.user2.id, channel_surface(self.channel.id), DEVICE)
        self.assertEqual(len(self._fan_out()), 1)

    def _comment_mirror(self, sender):
        """A real comment mirror: `metadata.taskCommentId` is what tells it
        apart from the task's own card message, which carries `task_id`
        too but is read in the channel."""
        return Message.objects.create(
            channel=self.channel,
            sender=sender,
            seq=2,
            body={"text": "c2"},
            body_text="c2",
            task_id=self.task.task_id,
            metadata={"taskCommentId": 77},
        )

    def test_a_comment_mention_is_suppressed_for_someone_on_the_task(self):
        mirror = self._comment_mirror(self.user)
        presence.mark_viewing(self.user2.id, task_surface(self.task.task_id), DEVICE)
        self.assertEqual(
            create_mention_activities(
                message=mirror, mentioned_user_ids=[str(self.user2.id)], actor=self.user
            ),
            [],
        )

    def test_a_comment_mention_is_written_for_someone_on_another_task(self):
        mirror = self._comment_mirror(self.user)
        presence.mark_viewing(self.user2.id, task_surface(self.task.task_id + 1), DEVICE)
        self.assertEqual(
            len(
                create_mention_activities(
                    message=mirror, mentioned_user_ids=[str(self.user2.id)], actor=self.user
                )
            ),
            1,
        )

    def test_a_comment_reaction_is_suppressed_for_someone_on_the_task(self):
        mirror = self._comment_mirror(self.user2)
        presence.mark_viewing(self.user2.id, task_surface(self.task.task_id), DEVICE)
        self.assertEqual(create_reaction_activity(message=mirror, emoji="👍", actor=self.user), [])

    def test_a_comment_reaction_is_written_when_the_task_is_closed(self):
        mirror = self._comment_mirror(self.user2)
        self.assertEqual(
            len(create_reaction_activity(message=mirror, emoji="👍", actor=self.user)), 1
        )


class MessageSurfaceTests(BaseAPITestCase):
    """Which surface each kind of message is judged against.

    Single-valued on purpose: a mention judged against a WIDER set than
    the thread-reply fan-out that follows it in `message_views` would not
    be silenced, it would be relabelled — the recipient loses the mention
    row and gains a thread-reply one.
    """

    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="C", owner=self.user
        )
        self.root = Message.objects.create(
            channel=self.channel, sender=self.user, seq=1, body={}, body_text="r"
        )
        # `Message.task_id` is a real FK, so the task has to exist.
        project = ProjectMaster.objects.create(team=self.team, project_name="P", owner=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=project, title="T", status="Open", reporter=self.user
        )

    def test_a_top_level_message_is_read_in_its_channel(self):
        self.assertEqual(message_surface(self.root), channel_surface(self.channel.id))

    def test_a_thread_reply_is_read_in_its_thread(self):
        reply = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=2,
            body={},
            body_text="re",
            thread_root=self.root,
            is_thread_reply=True,
        )
        self.assertEqual(message_surface(reply), thread_surface(self.root.id))

    def test_a_task_comment_is_read_on_its_task_not_in_the_thread(self):
        """The mirror is a thread reply AND carries a task id, so the
        precedence between those two branches is the whole point."""
        mirror = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=3,
            body={},
            body_text="c",
            task_id=self.task.task_id,
            thread_root=self.root,
            is_thread_reply=True,
            metadata={"taskCommentId": 9},
        )
        self.assertEqual(message_surface(mirror), task_surface(self.task.task_id))

    def test_a_task_card_message_is_read_in_its_channel(self):
        """`task_id` alone does not make a message a comment — the card
        that opens a task thread has one and is read in the chat."""
        card = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=4,
            body={},
            body_text="card",
            task_id=self.task.task_id,
        )
        self.assertEqual(message_surface(card), channel_surface(self.channel.id))


class SuppressedMentionIsNotRelabelledTests(BaseAPITestCase):
    """A silenced mention must not reappear under another type.

    `message_views` treats "was mentioned" as beating a thread-reply row.
    If it read that from the rows the mention producer RETURNED, then
    suppressing a mention would drop the recipient out of the exclude set
    and hand them a THREAD_REPLY row for the same message instead — the
    notification moved, not removed. This exercises the whole fan-out, so
    it fails if that bookkeeping ever goes back to counting rows.
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="C", owner=self.user
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="member")
        ChannelMember.objects.create(channel=self.channel, user=self.user2, role="member")
        self.root = Message.objects.create(
            channel=self.channel, sender=self.user2, seq=1, body=[], body_text="root"
        )

    def _send_mentioning_user2(self, parent_id):
        from origin.views.chat.message_views import (
            _allocate_seq_and_create_message_with_activities,
        )

        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "props": {"userId": str(self.user2.id), "user": "u2"}}
                ],
            }
        ]
        _msg, activities = _allocate_seq_and_create_message_with_activities(
            channel=self.channel,
            sender=self.user,
            body=body,
            body_text="@u2",
            parent_id=parent_id,
            metadata={},
        )
        return activities

    def _reply_mentioning_user2(self):
        return self._send_mentioning_user2(str(self.root.id))

    def test_a_mention_in_a_thread_beats_the_reply_row_as_before(self):
        """Baseline: nobody is looking, so exactly one MENTION and no
        THREAD_REPLY — the pre-existing 'mention beats reply' rule."""
        types = [a.activity_type for a in self._reply_mentioning_user2()]
        self.assertEqual(types, [ActivityType.MENTION])

    def test_reading_the_thread_leaves_no_row_of_any_type(self):
        presence.mark_viewing(self.user2.id, thread_surface(self.root.id), DEVICE)
        self.assertEqual(self._reply_mentioning_user2(), [])
        self.assertEqual(Activity.objects.filter(recipient=self.user2).count(), 0)

    def test_reading_only_the_channel_still_earns_the_mention(self):
        """The reply is behind a pane, so having the timeline open is not
        having seen it — and the row must be the MENTION, not a reply."""
        presence.mark_viewing(self.user2.id, channel_surface(self.channel.id), DEVICE)
        types = [a.activity_type for a in self._reply_mentioning_user2()]
        self.assertEqual(types, [ActivityType.MENTION])

    def test_a_mention_still_beats_the_plain_message_row(self):
        """Same rule on the other branch: a top-level @-mention produces
        the MENTION row and no MESSAGE row alongside it."""
        types = [a.activity_type for a in self._send_mentioning_user2(None)]
        self.assertEqual(types, [ActivityType.MENTION])

    def test_a_suppressed_mention_does_not_become_a_plain_message_row(self):
        presence.mark_viewing(self.user2.id, channel_surface(self.channel.id), DEVICE)
        self.assertEqual(self._send_mentioning_user2(None), [])
        self.assertEqual(Activity.objects.filter(recipient=self.user2).count(), 0)


class SurfaceTokenFormatTests(BaseAPITestCase):
    """The literal strings are a cross-repo contract with the frontend's
    `viewingSurface.ts`, which has the mirror of this test. They fail open
    (row written) if they drift, so pin them here."""

    def test_token_formats(self):
        self.assertEqual(channel_surface("abc-123"), "channel:abc-123")
        self.assertEqual(thread_surface("root-9"), "thread:root-9")
        self.assertEqual(task_surface(42), "task:42")

    def test_tokens_survive_the_surface_sanitizer(self):
        """A token the producers mint must be one the cache layer accepts —
        otherwise suppression would silently never fire."""
        for token in (
            channel_surface("11111111-2222-3333-4444-555555555555"),
            thread_surface("66666666-7777-8888-9999-000000000000"),
            task_surface(42),
        ):
            self.assertEqual(presence.clean_surface(token), token)
