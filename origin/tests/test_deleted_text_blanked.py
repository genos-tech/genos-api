"""MIP-1784: a deleted chat message or task comment keeps no text, and no read path sends any.

Before this, `DELETE /api/v3/messages/<id>/` and `soft_delete_comment` only flipped a flag. The row
kept its `body` / `body_text` / `comment_body`, and the incremental sync (`?since=`) re-served a
deleted message WITH that text to every member (it is how the client learns of the tombstone).

Two layers are pinned here:

  * the WRITERS blank the text in the same save that deletes, and keep the tombstone (id, `seq`,
    sender, timestamps, `deletedAt`) so threads, reply counts and the client's sync still work;
  * the READERS send no text for a deleted row even if a row was deleted before the writers did
    that (`legacy` below: `deleted_at` set by a queryset update, text still there). That is the
    state of production until `blank_deleted_text` has run, and a guard for any future writer
    that forgets.

The viewer in the read-path tests is `self.user2`, never the author.
"""

from __future__ import annotations

import json
from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Flag,
    Message,
    MessageAttachment,
    MessageMention,
    MessageReaction,
)
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.fetch_chat_thread import FETCH_CHAT_THREAD
from origin.search_engine.chunkers.chat_chunker import iter_gm_chunks
from origin.services import unified_writer
from origin.services.task_comment_manage import soft_delete_comment
from origin.tests.test_base import BaseAPITestCase

SECRET = "the-text-of-a-message-somebody-deleted"
MENTION_SECRET = "a-note-in-the-metadata"


def _blocks(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


class ChatBase(BaseAPITestCase):
    """`self.user` writes (the author), `self.user2` reads."""

    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="chat", owner=self.user
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")
        ChannelMember.objects.create(channel=self.channel, user=self.user2, role="member")
        self._seq = 0

    def say(self, text=SECRET, *, root=None, **extra):
        self._seq += 1
        return Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=self._seq,
            body=_blocks(text),
            body_text=text,
            thread_root=root,
            parent=root,
            is_thread_reply=root is not None,
            **extra,
        )

    def legacy_delete(self, message):
        """A message deleted the way the code did BEFORE this change: flag only, text kept."""
        now = timezone.now()
        Message.objects.filter(pk=message.pk).update(deleted_at=now, ts_updated_at=now)
        message.refresh_from_db()
        return message

    def full_json(self, res):
        return json.dumps(res.json() if hasattr(res, "json") else res.data, default=str)


class WritersBlankTheText(ChatBase):
    def setUp(self):
        super().setUp()
        self.authenticate()  # the author

    def delete(self, message):
        return self.client.delete(f"/api/v3/messages/{message.id}/")

    def test_deleting_a_message_blanks_its_text_and_keeps_the_tombstone(self):
        message = self.say(metadata={"taskId": 7, "note": MENTION_SECRET})
        MessageMention.objects.create(message=message, mentioned_user=self.user2)
        MessageAttachment.objects.create(
            message=message, uploader=self.user, file="chats/x/y.bin", original_name="salary.pdf"
        )
        before = message.ts_updated_at

        self.assertEqual(self.delete(message).status_code, 204)

        message.refresh_from_db()
        self.assertIsNotNone(message.deleted_at)
        self.assertEqual(message.body, [])
        self.assertEqual(message.body_text, "")
        # What the tombstone is made of stays.
        self.assertEqual(message.sender_id, self.user.id)
        self.assertEqual(message.seq, 1)
        self.assertEqual(message.channel_id, self.channel.id)
        self.assertEqual(message.metadata, {"taskId": 7})
        self.assertGreater(message.ts_updated_at, before)
        # Who was mentioned and what the file was called are part of the deleted content.
        self.assertFalse(MessageMention.objects.filter(message=message).exists())
        self.assertEqual(MessageAttachment.objects.get(message=message).original_name, "")

    def test_the_channel_owner_deleting_someone_elses_message_blanks_it_too(self):
        message = self.say()
        self.authenticate(self.user)  # the owner; the author is changed to user2
        Message.objects.filter(pk=message.pk).update(sender=self.user2)

        self.assertEqual(self.delete(message).status_code, 204)

        message.refresh_from_db()
        self.assertEqual((message.body, message.body_text), ([], ""))

    def test_a_thread_reply_is_blanked_and_still_counts_down_its_parent(self):
        root = self.say("root")
        reply = self.say(root=root)
        Message.objects.filter(pk=root.pk).update(reply_count=1)

        self.assertEqual(self.delete(reply).status_code, 204)

        reply.refresh_from_db()
        root.refresh_from_db()
        self.assertEqual((reply.body, reply.body_text), ([], ""))
        self.assertEqual(root.reply_count, 0)
        self.assertEqual(root.body_text, "root")

    def test_deleting_again_cleans_a_row_deleted_before_this_change(self):
        message = self.legacy_delete(self.say())
        self.assertEqual(message.body_text, SECRET)

        self.assertEqual(self.delete(message).status_code, 204)

        message.refresh_from_db()
        self.assertEqual((message.body, message.body_text), ([], ""))

    def test_the_next_sync_carries_the_blank_tombstone(self):
        message = self.say()
        checkpoint = timezone.now().isoformat()
        self.delete(message)

        self.authenticate(self.user2)
        res = self.client.get(
            f"/api/v3/channels/{self.channel.id}/messages/", {"since": checkpoint}
        )

        rows = res.data["data"]["messages"]
        self.assertEqual([r["id"] for r in rows], [str(message.id)])
        self.assertIsNotNone(rows[0]["deletedAt"])
        self.assertEqual((rows[0]["body"], rows[0]["bodyText"]), ([], ""))
        self.assertNotIn(SECRET, self.full_json(res))


class TaskCommentsAreBlankedWithTheirMirror(BaseAPITestCase):
    URL = "/api/v2/task/comment/"

    def setUp(self):
        super().setUp()
        self.authenticate()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open"
        )
        self.channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body=[],
            body_text="task",
            task_id=self.task.task_id,
        )

    def post(self, text=SECRET):
        res = self.client.post(
            self.URL,
            {
                "task_id": self.task.task_id,
                "sender_id": str(self.user.id),
                "comment_body": _blocks(text),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        return res.data["comment_id"]

    def delete(self, comment_id):
        return self.client.delete(f"{self.URL}?task_id={self.task.task_id}&comment_id={comment_id}")

    def mirror(self, comment_id):
        return Message.objects.get(
            id=unified_writer.task_comment_message_uuid(self.task.task_id, comment_id)
        )

    def test_the_rest_delete_blanks_the_comment_and_the_mirror_together(self):
        comment_id = self.post()
        self.assertEqual(self.mirror(comment_id).body_text, SECRET)

        self.assertEqual(self.delete(comment_id).status_code, 200)

        row = TaskComments.objects.get(task=self.task.task_id, comment_id=comment_id)
        self.assertTrue(row.is_deleted)
        self.assertEqual(row.comment_body, [])
        self.assertEqual(row.sender_id, self.user.id)  # the slot and its author stay
        mirror = self.mirror(comment_id)
        self.assertIsNotNone(mirror.deleted_at)
        self.assertEqual((mirror.body, mirror.body_text), ([], ""))
        # The marker that links the mirror to its comment is an id, not text: it stays.
        self.assertEqual(mirror.metadata.get("taskCommentId"), comment_id)

    def test_the_history_row_of_the_delete_still_carries_no_text(self):
        comment_id = self.post()
        self.delete(comment_id)
        rows = TaskActivity.objects.filter(
            task=self.task, action_type=TaskActivityActionType.COMMENT_DELETED
        )
        self.assertEqual(rows.count(), 1)
        self.assertNotIn(SECRET, json.dumps(rows.first().metadata, default=str))

    def test_an_edit_cannot_put_text_back_into_a_deleted_comment(self):
        comment_id = self.post()
        self.delete(comment_id)

        res = self.client.put(
            self.URL,
            {
                "task_id": self.task.task_id,
                "comment_id": comment_id,
                "comment_body": _blocks("written after the delete"),
            },
            format="json",
        )

        self.assertEqual(res.status_code, 404, res.data)
        row = TaskComments.objects.get(task=self.task.task_id, comment_id=comment_id)
        self.assertEqual(row.comment_body, [])
        self.assertEqual(self.mirror(comment_id).body_text, "")

    def test_deleting_twice_is_still_one_history_row(self):
        comment_id = self.post()
        self.delete(comment_id)
        self.assertEqual(self.delete(comment_id).status_code, 200)
        self.assertEqual(
            TaskActivity.objects.filter(
                task=self.task, action_type=TaskActivityActionType.COMMENT_DELETED
            ).count(),
            1,
        )

    def test_soft_delete_comment_cleans_a_comment_deleted_before_this_change(self):
        comment_id = self.post()
        TaskComments.objects.filter(task=self.task.task_id, comment_id=comment_id).update(
            is_deleted=True
        )
        TaskActivity.objects.all().delete()

        soft_delete_comment(TaskComments.objects.get(task=self.task.task_id, comment_id=comment_id))

        row = TaskComments.objects.get(task=self.task.task_id, comment_id=comment_id)
        self.assertEqual(row.comment_body, [])
        # No second "comment deleted" history row for a comment that was already deleted.
        self.assertEqual(TaskActivity.objects.count(), 0)


class ReadersSendNoTextForARowDeletedBefore(ChatBase):
    """Production state until `blank_deleted_text` has run: deleted, text still stored."""

    def setUp(self):
        super().setUp()
        self.checkpoint = (timezone.now() - timedelta(minutes=5)).isoformat()
        self.root = self.say("a live root")
        self.main = self.legacy_delete(self.say(metadata={"taskId": 7, "note": MENTION_SECRET}))
        self.reply = self.legacy_delete(self.say(root=self.root))
        MessageMention.objects.create(message=self.main, mentioned_user=self.user2)
        MessageAttachment.objects.create(
            message=self.main, uploader=self.user, file="chats/x/y.bin", original_name="salary.pdf"
        )
        self.authenticate(self.user2)

    def assert_clean(self, res):
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        dumped = self.full_json(res)
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn(MENTION_SECRET, dumped)
        self.assertNotIn("salary.pdf", dumped)
        self.assertNotIn("y.bin", dumped)

    def test_the_main_timeline_sync(self):
        res = self.client.get(
            f"/api/v3/channels/{self.channel.id}/messages/", {"since": self.checkpoint}
        )
        self.assert_clean(res)
        row = next(r for r in res.data["data"]["messages"] if r["id"] == str(self.main.id))
        # The tombstone still tells the client what it needs.
        self.assertIsNotNone(row["deletedAt"])
        self.assertEqual(row["sender"]["userId"], str(self.user.id))
        self.assertEqual(row["seq"], self.main.seq)
        self.assertEqual(row["body"], [])
        self.assertEqual(row["bodyText"], "")
        self.assertEqual(row["attachments"], [])
        self.assertEqual(row["mentions"], [])
        self.assertEqual(row["metadata"], {"taskId": 7})

    def test_the_thread_sync(self):
        res = self.client.get(
            f"/api/v3/channels/{self.channel.id}/threads/", {"since": self.checkpoint}
        )
        self.assert_clean(res)
        row = next(r for r in res.data["data"]["messages"] if r["id"] == str(self.reply.id))
        self.assertIsNotNone(row["deletedAt"])
        self.assertEqual(row["threadRootId"], str(self.root.id))

    def test_a_reaction_that_re_serves_a_deleted_message_brings_no_text(self):
        # Outside the `since` window by its own time; served again because a reaction changed.
        MessageReaction.objects.create(message=self.main, user=self.user, emoji="👍")
        Message.objects.filter(pk=self.main.pk).update(
            ts_updated_at=timezone.now() - timedelta(days=1)
        )
        res = self.client.get(
            f"/api/v3/channels/{self.channel.id}/messages/", {"since": self.checkpoint}
        )
        self.assert_clean(res)
        self.assertIn(str(self.main.id), [r["id"] for r in res.data["data"]["messages"]])

    def test_the_detail_endpoint(self):
        res = self.client.get(f"/api/v3/messages/{self.main.id}/")
        self.assert_clean(res)
        self.assertIsNotNone(res.data["deletedAt"])

    def test_the_flag_list_and_what_the_client_fetches_for_a_flag(self):
        Flag.objects.create(user=self.user2, message=self.main)
        self.assert_clean(self.client.get("/api/v3/flags/"))

    def test_the_channel_list(self):
        res = self.client.get("/api/v3/channels/", {"team_id": str(self.team.team_id)})
        self.assert_clean(res)

    def test_the_activity_feed(self):
        Activity.objects.create(
            team=self.team,
            recipient=self.user2,
            actor=self.user,
            activity_type=ActivityType.MENTION,
            channel=self.channel,
            message=self.main,
        )
        res = self.client.get("/api/v3/activities/", {"team_id": str(self.team.team_id)})
        self.assert_clean(res)
        self.assertEqual(len(res.json()["activities"]), 1)

    def test_the_activity_feed_serves_a_row_again_when_its_message_was_deleted_since(self):
        """A row the client cached before the delete would keep the text in its browser: the feed
        only ever served rows by `ts_created_at`, so a delete is told as the row served again."""
        live = self.say("seen before the delete")
        row = Activity.objects.create(
            team=self.team,
            recipient=self.user2,
            actor=self.user,
            activity_type=ActivityType.MENTION,
            channel=self.channel,
            message=live,
        )
        Activity.objects.filter(pk=row.pk).update(ts_created_at=timezone.now() - timedelta(days=3))
        since = timezone.now() - timedelta(minutes=5)

        res = self.client.get(
            "/api/v3/activities/", {"team_id": str(self.team.team_id), "since": since.isoformat()}
        )
        self.assertEqual(res.json()["activities"], [], "nothing changed yet")

        self.legacy_delete(live)
        res = self.client.get(
            "/api/v3/activities/", {"team_id": str(self.team.team_id), "since": since.isoformat()}
        )
        self.assert_clean(res)
        rows = res.json()["activities"]
        self.assertEqual([a["id"] for a in rows], [str(row.id)])
        self.assertIsNotNone(rows[0]["message"]["deletedAt"])

    def test_the_agent_reading_a_chat(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        out = FETCH_CHAT_THREAD.run({"chat_id": str(self.channel.id)}, ctx)
        self.assertNotIn(SECRET, json.dumps(out, default=str))
        out = FETCH_CHAT_THREAD.run(
            {"chat_id": str(self.channel.id), "thread_id": str(self.root.id)}, ctx
        )
        self.assertNotIn(SECRET, json.dumps(out, default=str))

    def test_the_search_index_text(self):
        for since in (None, timezone.now() - timedelta(days=1)):
            for batch in iter_gm_chunks(since=since):
                for chunk in batch.chunks:
                    self.assertNotIn(SECRET, chunk.search_text)
                    self.assertNotIn(SECRET, chunk.snippet_text or "")

    def test_a_deleted_task_comment_is_not_in_the_comment_list(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        task = TaskMaster.objects.create(
            team=self.team, project=project, title="T", status="Open", assignee=self.user2
        )
        TaskComments.objects.create(
            task=task,
            sender=self.user,
            comment_id=1,
            comment_body=_blocks(SECRET),
            is_deleted=True,
        )
        res = self.client.get("/api/v2/task/comment/", {"task_id": task.task_id})
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(SECRET, self.full_json(res))

    def test_a_tombstone_costs_no_query_of_its_own(self):
        """The delta is the hot read: a row being deleted instead of live adds no query."""
        path = f"/api/v3/channels/{self.channel.id}/messages/"
        one_more = self.say("one more row")

        def cost():
            with CaptureQueriesContext(connection) as ctx:
                res = self.client.get(path, {"since": self.checkpoint})
            self.assertEqual(res.status_code, 200)
            self.sql = [q["sql"][:150] for q in ctx.captured_queries]
            return len(ctx)

        cost()  # warm: caches that fill once (tier, content types)
        as_live = cost()
        sql_live = self.sql
        self.legacy_delete(one_more)
        self.assertEqual(cost(), as_live, "\n".join(self.sql) + "\n----\n" + "\n".join(sql_live))
