""" "Also send to <chat>" (MIP-2486): a thread reply that also shows in the main chat.

One `Message` row with `also_in_channel`, not a copy. The main timeline is "not a thread
reply, or also sent" (`main_timeline_q`): the delta, unread counts, latest message, app
badge, read cursors and the agent's chat reader follow it; each person is told once.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    ReadCursor,
)
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.fetch_chat_thread import _run as read_chat
from origin.services import presence, unread_badge, v3_activity, webpush_dispatch
from origin.services.schedule_delivery import _read_to_the_end
from origin.tests.test_base import BaseAPITestCase
from origin.views.chat.channel_views import _annotate_latest_and_unread
from origin.views.chat.read_cursor_views import mark_main_timeline_read

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _body(text="hi", mention=None):
    content = [{"type": "text", "text": text, "styles": {}}]
    if mention is not None:
        content.append(
            {"type": "mention", "props": {"userId": str(mention.id), "userName": mention.username}}
        )
    return [{"type": "paragraph", "content": content}]


class _AlsoSendBase(BaseAPITestCase):
    """A GM of three: `user` (owner) wrote the thread root, `user2` replies, `user3` is not in
    the thread."""

    kind = ChannelKind.GM

    def setUp(self):
        super().setUp()
        cache.clear()
        self.user3 = get_user_model().objects.create_user(
            username="third", email="third@example.com", password="pw123456"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user3)
        self.channel = Channel.objects.create(
            team=self.team, kind=self.kind, title="Room", owner=self.user
        )
        for u in (self.user, self.user2, self.user3):
            ChannelMember.objects.create(channel=self.channel, user=u, role="member")
        self.url = reverse("v3_messages_delta", args=[self.channel.id])
        self.root = self._send(self.user, "root")

    def _post(self, user, text="hi", *, parent=None, also=None, mention=None, extra=None):
        self.authenticate(user)
        data = {"body": _body(text, mention), "body_text": text}
        if parent is not None:
            data["parent_id"] = str(parent.id)
        if also is not None:
            data["also_in_channel"] = also
        data.update(extra or {})
        with mock.patch.object(webpush_dispatch, "dispatch_push_for_message") as plain:
            with self.captureOnCommitCallbacks(execute=True):
                res = self.client.post(self.url, data, format="json")
        self.plain_push = [str(r) for c in plain.call_args_list for r in c.args[1]]
        return res

    def _send(self, user, text="hi", **kwargs):
        res = self._post(user, text, **kwargs)
        self.assertEqual(res.status_code, 201, res.content)
        return Message.objects.get(id=res.data["id"])

    def _also(self, user=None, text="shared", **kwargs):
        return self._send(user or self.user2, text, parent=self.root, also=True, **kwargs)

    def _rows(self, message, user):
        return list(
            Activity.objects.filter(message=message, recipient=user).values_list(
                "activity_type", flat=True
            )
        )

    def _main_ids(self, user=None, since=None):
        self.authenticate(user or self.user)
        res = self.client.get(self.url, {"since": since} if since else {})
        self.assertEqual(res.status_code, 200, res.content)
        return {m["id"]: m for m in res.data["data"]["messages"]}


@override_settings(CACHES=LOCMEM)
class CreateAndReadTests(_AlsoSendBase):
    def test_one_row_in_the_thread_and_the_main_chat(self):
        reply = self._also()
        self.assertTrue(reply.is_thread_reply and reply.also_in_channel)
        self.root.refresh_from_db()
        self.assertEqual(self.root.reply_count, 1)
        main = self._main_ids()
        self.assertIn(str(reply.id), main)
        self.assertTrue(main[str(reply.id)]["alsoInChannel"])
        self.assertEqual(main[str(reply.id)]["threadRootId"], str(self.root.id))
        threads = self.client.get(f"/api/v3/channels/{self.channel.id}/threads/").data
        self.assertEqual([m["id"] for m in threads["data"]["messages"]], [str(reply.id)])

    def test_a_plain_reply_stays_out_of_the_main_chat(self):
        reply = self._send(self.user2, parent=self.root)
        self.assertFalse(reply.also_in_channel)
        self.assertNotIn(str(reply.id), self._main_ids())

    def test_only_true_counts(self):
        reply = self._send(self.user2, parent=self.root, also="yes")
        self.assertFalse(reply.also_in_channel)

    def test_refused_without_a_parent(self):
        before = Message.objects.count()
        res = self._post(self.user, also=True)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Message.objects.count(), before)

    def test_reply_to_a_reply_keeps_the_root(self):
        first = self._send(self.user2, parent=self.root)
        second = self._send(self.user, parent=first, also=True)
        self.assertEqual(second.thread_root_id, self.root.id)
        self.assertIn(str(second.id), self._main_ids())

    def test_resend_keeps_the_flag_and_tells_nobody_again(self):
        extra = {"correlation_id": "c-1"}
        first = self._also(extra=extra)
        rows = Activity.objects.count()
        res = self._post(self.user2, parent=self.root, also=True, extra=extra)
        self.assertEqual(res.data["id"], str(first.id))
        self.assertTrue(res.data["alsoInChannel"])
        self.assertEqual(Activity.objects.count(), rows)
        self.assertEqual(self.plain_push, [])

    def test_edit_cannot_change_the_flag(self):
        reply = self._also()
        res = self.client.patch(
            f"/api/v3/messages/{reply.id}/",
            {"body": _body("edited"), "body_text": "edited", "also_in_channel": False},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data["alsoInChannel"])

    def test_delete_reaches_the_main_chat_sync(self):
        reply = self._also()
        since = self.client.get(self.url).data["server_time"]
        self.authenticate(self.user2)
        self.assertEqual(self.client.delete(f"/api/v3/messages/{reply.id}/").status_code, 204)
        main = self._main_ids(since=since)
        self.assertIsNotNone(main[str(reply.id)]["deletedAt"])
        self.assertNotIn(str(reply.id), self._main_ids())

    def test_a_reaction_reaches_the_main_chat_sync(self):
        reply = self._also()
        old = Message.objects.filter(pk=reply.pk)
        since = self.client.get(self.url).data["server_time"]
        before = old.values_list("ts_updated_at", flat=True)[0]
        self.authenticate(self.user)
        res = self.client.post(
            f"/api/v3/messages/{reply.id}/reactions/", {"emoji": "👍"}, format="json"
        )
        self.assertIn(res.status_code, (200, 201), res.content)
        # Only the reaction changed: the row itself is older than the checkpoint.
        old.update(ts_updated_at=before)
        self.assertIn(str(reply.id), self._main_ids(since=since))

    def test_unread_latest_and_badge(self):
        ReadCursor.objects.create(
            user=self.user3, channel=self.channel, last_read_message=self.root
        )
        self.assertEqual(unread_badge._unread_chats(self.user3.id, str(self.team.team_id)), 0)
        self._send(self.user2, parent=self.root)
        row = _annotate_latest_and_unread(
            Channel.objects.filter(pk=self.channel.pk), self.user3
        ).get()
        self.assertEqual((row._unread_count, row._latest_seq), (0, self.root.seq))
        reply = self._also()
        row = _annotate_latest_and_unread(
            Channel.objects.filter(pk=self.channel.pk), self.user3
        ).get()
        self.assertEqual((row._unread_count, row._latest_seq), (1, reply.seq))
        self.assertEqual(unread_badge._unread_chats(self.user3.id, str(self.team.team_id)), 1)
        self.authenticate(self.user3)
        listed = self.client.get("/api/v3/channels/").data["channels"]
        mine = next(c for c in listed if c["id"] == str(self.channel.id))
        self.assertEqual(mine["latestMessage"]["id"], str(reply.id))

    def test_scheduled_post_does_not_read_past_an_unseen_one(self):
        ReadCursor.objects.create(
            user=self.user3, channel=self.channel, last_read_message=self.root
        )
        self.assertTrue(_read_to_the_end(self.user3.id, self.channel))
        self._also()
        self.assertFalse(_read_to_the_end(self.user3.id, self.channel))

    def test_reading_the_main_chat_clears_its_feed_row(self):
        reply = self._also()
        for user, kind in (
            (self.user, ActivityType.THREAD_REPLY),
            (self.user3, ActivityType.MESSAGE),
        ):
            self.authenticate(user)
            res = self.client.put(
                f"/api/v3/channels/{self.channel.id}/read_cursor/",
                {"last_read_message_id": str(reply.id)},
                format="json",
            )
            self.assertEqual(res.status_code, 200, res.content)
            row = Activity.objects.get(message=reply, recipient=user)
            self.assertEqual((row.activity_type, row.is_read), (kind, True))

    def test_reading_the_thread_clears_it_too(self):
        reply = self._also()
        self.authenticate(self.user)
        self.client.put(
            f"/api/v3/channels/{self.channel.id}/read_cursor/",
            {"last_read_message_id": str(reply.id), "thread_root_id": str(self.root.id)},
            format="json",
        )
        self.assertTrue(Activity.objects.get(message=reply, recipient=self.user).is_read)

    def test_hiding_the_chat_reads_it(self):
        reply = self._also()
        mark_main_timeline_read(user=self.user3, channel=self.channel)
        cursor = ReadCursor.objects.get(
            user=self.user3, channel=self.channel, thread_root__isnull=True
        )
        self.assertEqual(cursor.last_read_message_id, reply.id)

    def test_agent_reads_it_in_the_chat_with_its_thread(self):
        self._send(self.user2, "private to the thread", parent=self.root)
        reply = self._also()
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        result = read_chat({"chat_id": str(self.channel.id)}, ctx)
        rows = {m["message_id"]: m for m in result["messages"]}
        self.assertEqual(set(rows), {str(self.root.id), str(reply.id)})
        self.assertEqual(rows[str(reply.id)]["in_thread"], str(self.root.id))
        self.assertNotIn("in_thread", rows[str(self.root.id)])


@override_settings(CACHES=LOCMEM)
class NotifiedOnceTests(_AlsoSendBase):
    def test_participant_gets_the_thread_reply_and_others_the_message(self):
        reply = self._also()
        self.assertEqual(self._rows(reply, self.user), [ActivityType.THREAD_REPLY])
        self.assertEqual(self._rows(reply, self.user3), [ActivityType.MESSAGE])
        self.assertEqual(self._rows(reply, self.user2), [])
        self.assertEqual(self.plain_push, [str(self.user3.id)])

    def test_plain_reply_tells_nobody_outside_the_thread(self):
        reply = self._send(self.user2, parent=self.root)
        self.assertEqual(self._rows(reply, self.user3), [])
        self.assertEqual(self.plain_push, [])

    def test_a_mention_is_the_only_notification(self):
        reply = self._also(mention=self.user3)
        self.assertEqual(self._rows(reply, self.user3), [ActivityType.MENTION])
        self.assertEqual(self.plain_push, [])

    def test_participant_reading_the_thread_gets_nothing_else(self):
        presence.mark_viewing(self.user.id, v3_activity.thread_surface(self.root.id))
        reply = self._also()
        self.assertEqual(self._rows(reply, self.user), [])
        self.assertEqual(self.plain_push, [str(self.user3.id)])


@override_settings(CACHES=LOCMEM)
class DirectMessageTests(_AlsoSendBase):
    kind = ChannelKind.MDM

    def test_everyone_is_already_in_the_thread(self):
        reply = self._also()
        self.assertEqual(self._rows(reply, self.user), [ActivityType.THREAD_REPLY])
        self.assertEqual(self._rows(reply, self.user3), [ActivityType.THREAD_REPLY])
        self.assertEqual(self.plain_push, [])


class ProjectChatTests(BaseAPITestCase):
    def test_a_project_chat_refuses_it(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        channel = Channel.objects.get(project_id=project.project_id, kind=ChannelKind.PM)
        ProjectMembers.objects.get_or_create(team=self.team, project=project, attendee=self.user)
        ChannelMember.objects.get_or_create(channel=channel, user=self.user)
        root = Message.objects.create(
            channel=channel, sender=self.user, seq=1, body=_body(), body_text="hi"
        )
        self.authenticate(self.user)
        res = self.client.post(
            reverse("v3_messages_delta", args=[channel.id]),
            {"body": _body(), "parent_id": str(root.id), "also_in_channel": True},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(Message.objects.filter(channel=channel).count(), 1)
