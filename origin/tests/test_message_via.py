"""MIP-1570: the chat shows the official Claude / ChatGPT icon on what a connected app wrote.

A chat `Message` is serialised ONCE and relayed unchanged to the whole channel room (genos-sockets:
`message.send` and `realtime_push.push_message`), so the form stored in `Message.metadata.via` is
the form everyone in the channel may read: `{"kind": "oauth"}` plus `"app": "claude" | "chatgpt"`
when the client is an official one. Never a name, a connection id, a key prefix.

Two writers stamp it, both server-side and both only when the write came through a connected app:

  * the mirror of a task comment (`unified_writer.write_task_comment_as_thread_reply`), and
  * the card posted for a task or milestone made over MCP (`task_card_post`).

Nothing a client sends can: every path that takes client `metadata` drops a `via` from it, a person
who edits an app-written message drops the stamp, and the serializer reduces once more (a stored
`via` that is not a connected app's is not shown at all).
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from origin.middleware.current_user import via_override
from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.fetch_chat_thread import FETCH_CHAT_THREAD
from origin.search_engine.agent.tools.list_my_chats import LIST_MY_CHATS
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.serializers.chat.unified_serializers import MessageSerializer
from origin.services import (
    briefing,
    item_lines,
    message_via,
    task_card_post,
    timeline,
    unified_writer,
)
from origin.services.task_comment_fanout import fan_out_task_comment
from origin.services.task_comment_manage import edit_comment
from origin.tests.test_base import BaseAPITestCase

CLAUDE = {
    "kind": "oauth",
    "connection_id": "conn-77-secret",
    "client_name": "Alices private Claude",
    "app": "claude",
}
CHATGPT = {"kind": "oauth", "connection_id": "conn-78-secret", "client_name": "G", "app": "chatgpt"}
OTHER_APP = {"kind": "oauth", "connection_id": "conn-79-secret", "client_name": "Some Client"}
KEY = {"kind": "api_key", "key_id": "k-1", "key_name": "Alices phone", "key_prefix": "gk_alice"}
REDUCED_CLAUDE = {"kind": "oauth", "app": "claude"}
SECRETS = (
    "conn-77-secret",
    "conn-78-secret",
    "conn-79-secret",
    "Alices private Claude",
    "Some Client",
    "Alices phone",
    "gk_alice",
    "connection_id",
    "client_name",
    "key_name",
)
BODY = [{"type": "paragraph", "content": [{"type": "text", "text": "mirror-comment-text"}]}]
PLANTS = [
    {"kind": "oauth", "app": "claude"},
    {"kind": "oauth", "app": "chatgpt", "connection_id": "x"},
    CLAUDE,
    {"kind": "agent"},
    "claude",
    ["oauth"],
    True,
]


class _Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Site",
            code="SIT",
            owner=self.user,
            project_system_user=self.user,
        )
        for member in (self.user, self.user2):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=member)
        self.channel = Channel.objects.get(kind=ChannelKind.PM, legacy_chat_id=self.project.pk)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.pushes = mock.patch.object(task_card_post.realtime_push, "push_message").start()
        mock.patch.object(task_card_post.realtime_push, "push_activity").start()
        self.addCleanup(mock.patch.stopall)
        self.next_comment = 0

    # -- helpers -------------------------------------------------------------------------------

    def make_task(self, source=None):
        """A task with its card, made through `source` (a connected app, a key, nothing)."""
        if source is None:
            out = CREATE_TASK.run(
                {"title": "Ship", "project_id": self.project.project_id}, self.ctx
            )
        else:
            with via_override(source):
                out = CREATE_TASK.run(
                    {"title": "Ship", "project_id": self.project.project_id}, self.ctx
                )
        return TaskMaster.objects.get(task_id=out["task_id"])

    def card_of(self, task):
        return Message.objects.get(
            channel=self.channel, task_id=task.task_id, is_thread_reply=False
        )

    def comment(self, task, source=None, sender=None):
        """Save a task comment and run the fan-out that mirrors it, as `source` wrote it."""
        self.next_comment += 1
        comment = TaskComments.objects.create(
            task=task,
            comment_id=self.next_comment,
            sender=sender or self.user,
            comment_body=BODY,
        )
        if source is None:
            fan_out_task_comment(comment)
        else:
            with via_override(source):
                fan_out_task_comment(comment)
        mirror = Message.objects.get(
            id=unified_writer.task_comment_message_uuid(task.task_id, comment.comment_id)
        )
        return comment, mirror

    def wire(self, message):
        """What the chat room and the chat list carry for `message`."""
        fresh = Message.objects.get(pk=message.pk)
        return json.loads(json.dumps(MessageSerializer(fresh).data, default=str))


class TheMirroredCommentIsStamped(_Base):
    def test_a_comment_written_through_claude_carries_the_reduced_via(self):
        _, mirror = self.comment(self.make_task(), CLAUDE)
        self.assertEqual(
            mirror.metadata,
            {"taskCommentId": mirror.metadata["taskCommentId"], "via": REDUCED_CLAUDE},
        )

    def test_chatgpt_and_an_unofficial_connected_app(self):
        task = self.make_task()
        _, gpt = self.comment(task, CHATGPT)
        _, other = self.comment(task, OTHER_APP)
        self.assertEqual(gpt.metadata["via"], {"kind": "oauth", "app": "chatgpt"})
        self.assertEqual(other.metadata["via"], {"kind": "oauth"})

    def test_a_planted_family_is_not_kept(self):
        _, mirror = self.comment(
            self.make_task(), {**OTHER_APP, "app": "claude-official", "extra": 1}
        )
        self.assertEqual(mirror.metadata["via"], {"kind": "oauth"})

    def test_nothing_private_is_stored_or_served(self):
        _, mirror = self.comment(self.make_task(), CLAUDE)
        text = repr(mirror.metadata) + json.dumps(self.wire(mirror))
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_anything_but_a_connected_app_stamps_nothing(self):
        task = self.make_task()
        for source in (None, KEY, {"kind": "agent"}, {"kind": "app"}, {"kind": "unknown"}):
            _, mirror = self.comment(task, source)
            self.assertNotIn("via", mirror.metadata, source)
            self.assertEqual(set(mirror.metadata), {"taskCommentId"})

    def test_the_lazily_made_card_header_is_not_the_apps_card(self):
        """A comment on a task with no card makes its card; the app wrote the comment, not the
        card, so the card is unstamped."""
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="No card", status="Open"
        )
        self.comment(task, CLAUDE)
        metadata = self.card_of(task).metadata
        self.assertEqual(metadata["taskId"], task.task_id)
        self.assertNotIn("via", metadata)


class TheCardIsStamped(_Base):
    def test_a_task_made_through_claude_gets_a_stamped_card(self):
        task = self.make_task(CLAUDE)
        card = self.card_of(task)
        self.assertEqual(card.metadata["via"], REDUCED_CLAUDE)
        # The rest of the card is exactly what it was.
        self.assertEqual(
            {k: v for k, v in card.metadata.items() if k != "via"},
            {
                "taskId": task.task_id,
                "displayId": task.display_id,
                "taskStatus": "Open",
                "systemUserId": str(self.project.project_system_user_id),
            },
        )

    def test_a_card_made_any_other_way_is_unstamped(self):
        for source in (None, KEY, {"kind": "agent"}):
            card = self.card_of(self.make_task(source))
            self.assertNotIn("via", card.metadata, source)

    def test_the_card_never_carries_a_name(self):
        card = self.card_of(self.make_task(CLAUDE))
        text = repr(card.metadata) + json.dumps(self.wire(card))
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_a_server_refresh_keeps_the_stamp_and_the_rest_of_the_metadata(self):
        task = self.make_task(CLAUDE)
        Message.objects.filter(pk=self.card_of(task).pk).update(
            metadata={**self.card_of(task).metadata, "fromBrowser": True}
        )
        with self.captureOnCommitCallbacks(execute=True):
            UPDATE_TASK.run({"task_id": task.task_id, "status": "WIP", "reason": "Test."}, self.ctx)
        card = self.card_of(task)
        self.assertEqual(card.metadata["via"], REDUCED_CLAUDE)
        self.assertTrue(card.metadata["fromBrowser"])
        self.assertEqual(card.metadata["taskStatus"], "WIP")


class TheChatListAndTheRoomCarryIt(_Base):
    def test_latest_message_of_the_chat_list_has_the_via_at_metadata_via(self):
        self.make_task(CLAUDE)
        self.authenticate(self.user2)
        res = self.client.get(reverse("v3_channel_list"))
        self.assertEqual(res.status_code, 200, res.content)
        rows = res.data["channels"] if isinstance(res.data, dict) else res.data
        (row,) = [r for r in rows if r["id"] == str(self.channel.id)]
        self.assertEqual(row["latestMessage"]["metadata"]["via"], REDUCED_CLAUDE)
        text = json.dumps(res.data, default=str)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_the_message_pushed_to_the_room_carries_it(self):
        task = self.make_task(CLAUDE)
        pushed = [c.args[0] for c in self.pushes.call_args_list]
        self.assertTrue(pushed)
        # `push_message` serialises the stored row once, with no viewer.
        self.assertEqual(self.wire(pushed[-1])["metadata"]["via"], REDUCED_CLAUDE)
        self.assertEqual(pushed[-1].task_id, task.task_id)

    def test_a_thread_reply_carries_it_to_a_colleague(self):
        task = self.make_task()
        _, mirror = self.comment(task, CLAUDE)
        self.authenticate(self.user2)
        res = self.client.get(reverse("v3_thread_messages_delta", args=[self.channel.id]))
        self.assertEqual(res.status_code, 200, res.content)
        (row,) = [m for m in res.data["data"]["messages"] if m["id"] == str(mirror.id)]
        self.assertEqual(row["metadata"]["via"], REDUCED_CLAUDE)


class TheSerializerReducesAgain(_Base):
    """A stored `via` is shown only as a connected app's reduced kind and family."""

    def test_a_full_stored_via_is_reduced(self):
        _, mirror = self.comment(self.make_task(), None)
        Message.objects.filter(pk=mirror.pk).update(metadata={"taskCommentId": 1, "via": CLAUDE})
        shown = self.wire(mirror)["metadata"]
        self.assertEqual(shown, {"taskCommentId": 1, "via": REDUCED_CLAUDE})
        for secret in SECRETS:
            self.assertNotIn(secret, json.dumps(shown))

    def test_a_via_that_is_not_a_connected_apps_is_not_shown(self):
        _, mirror = self.comment(self.make_task(), None)
        for planted in (KEY, {"kind": "agent"}, {"kind": "app"}, "claude", ["oauth"], 3, {}):
            Message.objects.filter(pk=mirror.pk).update(
                metadata={"taskCommentId": 1, "via": planted}
            )
            self.assertEqual(self.wire(mirror)["metadata"], {"taskCommentId": 1}, planted)

    def test_the_family_switch_acts_on_what_is_stored(self):
        _, mirror = self.comment(self.make_task(), CLAUDE)
        with mock.patch("origin.views.public.mcp.via_privacy.SHOW_OAUTH_APP_TO_EVERYONE", False):
            self.assertEqual(self.wire(mirror)["metadata"]["via"], {"kind": "oauth"})

    def test_an_old_message_is_served_exactly_as_it_was(self):
        _, mirror = self.comment(self.make_task(), None)
        for stored in (
            {},
            {"taskCommentId": 1},
            {"taskId": 4, "displayId": "A-1", "x": [1, {"y": 2}]},
        ):
            Message.objects.filter(pk=mirror.pk).update(metadata=stored)
            self.assertEqual(self.wire(mirror)["metadata"], stored)

    def test_the_stored_row_is_not_edited_by_serialising_it(self):
        _, mirror = self.comment(self.make_task(), None)
        Message.objects.filter(pk=mirror.pk).update(metadata={"via": CLAUDE})
        MessageSerializer(Message.objects.get(pk=mirror.pk)).data  # noqa: B018
        self.assertEqual(Message.objects.get(pk=mirror.pk).metadata, {"via": CLAUDE})


class AClientCannotPlantOne(_Base):
    """Every path that takes client `metadata` drops a `via` from it."""

    def setUp(self):
        super().setUp()
        self.task = self.make_task()
        self.card = self.card_of(self.task)
        self.url = reverse("v3_messages_delta", args=[self.channel.id])
        self.authenticate()

    def _post(self, metadata, **extra):
        res = self.client.post(
            self.url,
            {"body": BODY, "body_text": "hi", "metadata": metadata, **extra},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        return res

    def test_rest_post_drops_it_from_the_row_and_the_response(self):
        for planted in PLANTS:
            res = self._post({"keep": "me", "via": planted})
            row = Message.objects.get(pk=res.data["id"])
            self.assertEqual(row.metadata, {"keep": "me"}, planted)
            self.assertNotIn("via", res.data["metadata"], planted)

    def test_rest_post_keeps_the_task_id_the_card_needs(self):
        # A task with no card yet: one that has a card is answered with it (MIP-2483).
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="No card", status="Open"
        )
        res = self._post({"taskId": task.task_id, "via": CLAUDE})
        row = Message.objects.get(pk=res.data["id"])
        self.assertEqual(row.metadata, {"taskId": task.task_id})
        self.assertEqual(row.task_id, task.task_id)

    def test_a_thread_reply_post_drops_it(self):
        res = self._post({"via": CLAUDE}, parent_id=str(self.card.id))
        self.assertEqual(Message.objects.get(pk=res.data["id"]).metadata, {})

    def test_a_post_made_while_the_session_reports_a_connected_app_is_still_not_stamped(self):
        """The REST send is a person's message: it never stamps, whatever the source says."""
        with via_override(CLAUDE):
            res = self._post({"via": CLAUDE})
        self.assertEqual(Message.objects.get(pk=res.data["id"]).metadata, {})

    def test_the_idempotent_resend_stays_clean(self):
        first = self._post({"via": CLAUDE}, correlation_id="c-1")
        again = self._post({"via": CLAUDE}, correlation_id="c-1")
        self.assertEqual(first.data["id"], again.data["id"])
        self.assertNotIn("via", Message.objects.get(pk=first.data["id"]).metadata)

    def test_a_refresh_keeps_the_creations_stamp(self):
        """A card the server rebuilds is still the one made through that app."""
        task = self.make_task(CLAUDE)
        stamped = self.card_of(task)
        TaskMaster.objects.filter(pk=task.pk).update(title="edited")
        task_card_post.refresh_task_card(task.pk)
        row = Message.objects.get(pk=stamped.pk)
        self.assertIn("edited", row.body_text)
        self.assertEqual(row.metadata["via"], REDUCED_CLAUDE)

    def test_rewrite_card_message_strips_unless_the_server_built_the_metadata(self):
        from origin.views.chat.message_views import rewrite_card_message

        rewrite_card_message(self.card, body=BODY, body_text="x", metadata={"a": 1, "via": CLAUDE})
        self.assertEqual(Message.objects.get(pk=self.card.pk).metadata, {"a": 1})
        rewrite_card_message(
            self.card,
            body=BODY,
            body_text="x",
            metadata={"a": 1, "via": REDUCED_CLAUDE},
            server_built=True,
        )
        self.assertEqual(Message.objects.get(pk=self.card.pk).metadata["via"], REDUCED_CLAUDE)


class AnEditByAPersonDropsTheStamp(_Base):
    def test_editing_the_message_drops_it(self):
        _, mirror = self.comment(self.make_task(), CLAUDE, sender=self.user)
        self.assertIn("via", Message.objects.get(pk=mirror.pk).metadata)
        self.authenticate(self.user)
        res = self.client.patch(
            reverse("v3_message_detail", args=[mirror.id]),
            {"body": BODY, "body_text": "edited"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(
            Message.objects.get(pk=mirror.pk).metadata,
            {"taskCommentId": mirror.metadata["taskCommentId"]},
        )
        self.assertNotIn("via", res.data["metadata"])

    def test_editing_a_message_with_no_stamp_writes_no_metadata(self):
        _, mirror = self.comment(self.make_task(), None)
        self.authenticate(self.user)
        with mock.patch.object(Message, "save", autospec=True, side_effect=Message.save) as save:
            self.client.patch(
                reverse("v3_message_detail", args=[mirror.id]),
                {"body": BODY, "body_text": "edited"},
                format="json",
            )
        fields = save.call_args.kwargs.get("update_fields")
        self.assertNotIn("metadata", fields)

    def test_editing_the_comment_in_the_app_drops_it(self):
        task = self.make_task()
        comment, mirror = self.comment(task, CLAUDE)
        self.authenticate(self.user)
        res = self.client.put(
            "/api/v2/task/comment/",
            {
                "task_id": task.task_id,
                "comment_id": comment.comment_id,
                "comment_body": BODY + [{"type": "paragraph"}],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertNotIn("via", Message.objects.get(pk=mirror.pk).metadata)

    def test_the_mirror_edit_stamps_whoever_wrote_the_new_text(self):
        task = self.make_task()
        comment, mirror = self.comment(task, None)
        with via_override(CLAUDE):
            unified_writer.update_task_comment_thread_reply(
                task_id=task.task_id, comment_id=comment.comment_id, body=BODY
            )
        self.assertEqual(Message.objects.get(pk=mirror.pk).metadata["via"], REDUCED_CLAUDE)
        with via_override(CHATGPT):
            unified_writer.update_task_comment_thread_reply(
                task_id=task.task_id, comment_id=comment.comment_id, body=BODY
            )
        self.assertEqual(
            Message.objects.get(pk=mirror.pk).metadata["via"], {"kind": "oauth", "app": "chatgpt"}
        )
        unified_writer.update_task_comment_thread_reply(
            task_id=task.task_id, comment_id=comment.comment_id, body=BODY
        )
        self.assertEqual(
            Message.objects.get(pk=mirror.pk).metadata, {"taskCommentId": comment.comment_id}
        )

    def test_an_app_edit_through_edit_comment_keeps_its_stamp(self):
        task = self.make_task()
        comment, mirror = self.comment(task, CLAUDE)
        with via_override(CLAUDE):
            edit_comment(comment, BODY + [{"type": "paragraph"}])
        self.assertEqual(Message.objects.get(pk=mirror.pk).metadata["via"], REDUCED_CLAUDE)


class TheModelSeesNoDifference(_Base):
    maxDiff = None

    """The in-app model's chat readers read named keys of `metadata`; a stamp changes nothing."""

    def _everything(self):
        now = self.now
        since, until = now - timedelta(days=1), now + timedelta(days=1)
        thread = FETCH_CHAT_THREAD.run(
            {"chat_id": str(self.channel.id), "thread_id": str(self.parent.id)}, self.ctx
        )
        main = FETCH_CHAT_THREAD.run({"chat_id": str(self.channel.id)}, self.ctx)
        chats = LIST_MY_CHATS.run({}, self.ctx)
        line = item_lines.item_lines(self.ctx, self.task.task_id, since, until, now=now)
        events = timeline.timeline(
            self.ctx,
            "task",
            str(self.task.task_id),
            since=since.isoformat(),
            until=until.isoformat(),
        )
        events.pop("window", None)  # its `until` is clamped to the wall clock
        brief = briefing.build_briefing(self.ctx, window="week", now=now)
        return json.loads(json.dumps([thread, main, chats, line, events, brief], default=str))

    def test_with_and_without_a_via_the_tool_results_are_byte_identical(self):
        self.now = timezone.now() + timedelta(hours=1)
        self.task = self.make_task()
        self.parent = self.card_of(self.task)
        _, mirror = self.comment(self.task, None)
        without = self._everything()
        self.assertIn("mirror-comment-text", json.dumps(without), "the comment was read")
        Message.objects.filter(pk=mirror.pk).update(
            metadata={**mirror.metadata, "via": REDUCED_CLAUDE}
        )
        Message.objects.filter(pk=self.parent.pk).update(
            metadata={**self.parent.metadata, "via": REDUCED_CLAUDE}
        )
        self.assertEqual(self._everything(), without)
        self.assertNotIn("claude", json.dumps(without))


class TheHelper(BaseAPITestCase):
    def test_without_via_never_edits_its_input(self):
        meta = {"a": 1, "via": {"kind": "oauth"}}
        self.assertEqual(message_via.without_via(meta), {"a": 1})
        self.assertEqual(meta, {"a": 1, "via": {"kind": "oauth"}})
        for other in (None, [], "x", {}):
            self.assertEqual(message_via.without_via(other), other)

    def test_stamped_replaces_an_old_via_and_adds_the_writes_own(self):
        with via_override(CLAUDE):
            self.assertEqual(
                message_via.stamped({"a": 1, "via": {"kind": "oauth", "app": "chatgpt"}}),
                {"a": 1, "via": REDUCED_CLAUDE},
            )
        self.assertEqual(message_via.stamped({"a": 1, "via": {"kind": "oauth"}}), {"a": 1})
