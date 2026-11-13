"""The closing line in the thread a task was made from (MIP-1283, route (b)).

Behind `AGENT_CLOSING_LINE_IN_ORIGIN_THREAD` (default off): it adds chat content the in-app
agent's `fetch_chat_thread` returns, so it waits for the grouped eval. See `origin/services/task_close_notice.py` for the design."""

from __future__ import annotations

import hashlib
import json
import os
import re
from unittest import mock

from django.conf import settings
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from origin.models.chat.unified_models import (
    Activity,
    Channel,
    ChannelKind,
    Message,
    MessageMention,
)
from origin.models.common.team_models import TeamMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster
from origin.services import task_close_notice
from origin.tests.test_briefing import BriefingBase

TITLE = "Quarterly figures (confidential)"


class CloseNoticeBase(BriefingBase):
    def setUp(self):
        super().setUp()
        # What these tests are about is the line, so they run with its flag ON; the flag-off
        # contract is `TestFlagOffIsMain` below.
        self.enterContext(
            override_settings(
                SEARCH_ENGINE={
                    **settings.SEARCH_ENGINE,
                    "AGENT_CLOSING_LINE_IN_ORIGIN_THREAD": True,
                }
            )
        )

    def thread_and_task(self, kind=ChannelKind.GM, **task_args):
        """A client (user2) asks in a chat; the PM (user) makes a task of the thread."""
        channel = self.channel([self.user, self.user2], kind=kind)
        root = self.message(channel, self.user2, "Please fix Y", at=self.ago(hours=9))
        task = self.make_task(
            TITLE,
            assignee=self.user,
            reporter=self.user,
            chat_id=str(channel.id),
            thread_id=str(root.id),
            **task_args,
        )
        return channel, root, task

    def close(self, task):
        task.status = "Closed"
        task.save()

    def notices(self, channel=None):
        qs = Message.objects.filter(metadata__systemNotice=task_close_notice.NOTICE_KIND)
        return list(qs.filter(channel=channel) if channel else qs)


class TestTheLine(CloseNoticeBase):
    def test_closing_the_task_says_so_once_in_the_thread_it_came_from(self):
        channel, root, task = self.thread_and_task()
        self.close(task)
        (line,) = self.notices(channel)
        self.assertEqual(line.body_text, f"Task {task.display_id} is now Closed.")
        self.assertIsNone(line.sender_id)  # nobody is impersonated
        self.assertEqual((line.parent_id, line.thread_root_id), (root.id, root.id))
        self.assertTrue(line.is_thread_reply)
        root.refresh_from_db()
        self.assertEqual(root.reply_count, 1)

    def test_it_rings_nobody_and_is_not_a_task_card(self):
        channel, root, task = self.thread_and_task()
        before = Activity.objects.count()
        self.close(task)
        (line,) = self.notices(channel)
        self.assertEqual(Activity.objects.count(), before)  # no sidebar row, so no push either
        self.assertFalse(MessageMention.objects.filter(message=line).exists())
        self.assertIsNone(line.task_id)  # the one-task-per-thread guard reads the card
        self.assertNotIn("taskId", line.metadata)  # and so does the PM "one bubble per task"
        self.assertNotIn("mention", json.dumps(line.body))

    def test_the_title_is_not_in_the_line(self):
        channel, _, task = self.thread_and_task()
        self.close(task)
        (line,) = self.notices(channel)
        self.assertNotIn("Quarterly", json.dumps([line.body, line.body_text, line.metadata]))

    def test_the_thread_still_belongs_to_one_task(self):
        from origin.services.thread_link import find_thread_link_conflict

        channel, root, task = self.thread_and_task()
        self.close(task)
        self.assertEqual(
            find_thread_link_conflict(self.team.team_id, channel.id, root.id), task.task_id
        )
        self.assertEqual(TaskMaster.objects.filter(thread_id=str(root.id)).count(), 1)

    def test_a_retry_of_the_same_close_collapses_and_a_second_close_says_it_again(self):
        channel, _, task = self.thread_and_task()
        self.close(task)
        first = TaskActivity.objects.get(task=task, new_value="Closed")
        task_close_notice.post_close_notice(task, first.pk)  # the same close, delivered twice
        self.assertEqual(len(self.notices(channel)), 1)
        task.status = "WIP"
        task.save()
        self.assertEqual(len(self.notices(channel)), 1)  # reopening says nothing
        self.close(task)  # a second close is second news
        self.assertEqual(len(self.notices(channel)), 2)

    def test_saving_a_closed_task_again_says_nothing_more(self):
        channel, _, task = self.thread_and_task()
        self.close(task)
        task.title = "Renamed"
        task.save()
        self.assertEqual(len(self.notices(channel)), 1)

    def test_other_status_changes_say_nothing(self):
        channel, _, task = self.thread_and_task()
        for status in ("WIP", "Blocked", "Pending"):
            task.status = status
            task.save()
        self.assertEqual(self.notices(channel), [])

    def test_a_dm_and_an_mdm_thread_hear_it_too(self):
        for kind in (ChannelKind.DM, ChannelKind.MDM):
            channel, _, task = self.thread_and_task(kind=kind)
            self.close(task)
            self.assertEqual(len(self.notices(channel)), 1, kind)


class TestWhereItIsPosted(CloseNoticeBase):
    def closed_in_nowhere(self, task):
        self.close(task)
        return self.notices()

    def test_a_task_with_no_thread_posts_nothing(self):
        task = self.make_task("Plain")
        self.assertEqual(self.closed_in_nowhere(task), [])

    def test_a_legacy_numeric_thread_id_posts_nothing(self):
        channel = self.channel([self.user, self.user2])
        task = self.make_task("Old", chat_id=str(channel.id), thread_id="12345")
        self.assertEqual(self.closed_in_nowhere(task), [])

    def test_a_chat_id_that_is_not_the_threads_channel_posts_nothing(self):
        """Old rows point at another channel than their thread's: left alone."""
        channel, root, _ = self.thread_and_task()
        other = self.channel([self.user, self.user2], title="Elsewhere")
        task = self.make_task("Mismatch", chat_id=str(other.id), thread_id=str(root.id))
        self.assertEqual(self.closed_in_nowhere(task), [])

    def test_a_deleted_root_or_a_reply_posts_nothing(self):
        channel, root, task = self.thread_and_task()
        reply = self.message(channel, self.user, "re", root=root, at=self.ago(hours=8))
        as_reply = self.make_task("On a reply", chat_id=str(channel.id), thread_id=str(reply.id))
        Message.objects.filter(pk=root.pk).update(deleted_at=self.now)
        self.assertEqual(self.closed_in_nowhere(task), [])
        self.assertEqual(self.closed_in_nowhere(as_reply), [])

    def test_a_pm_channel_and_a_deleted_channel_post_nothing(self):
        channel, root, task = self.thread_and_task(kind=ChannelKind.PM)
        self.assertEqual(self.closed_in_nowhere(task), [])
        channel2, _, task2 = self.thread_and_task()
        Channel.objects.filter(pk=channel2.pk).update(is_deleted=True)
        self.assertEqual(self.closed_in_nowhere(task2), [])

    def test_a_channel_of_another_team_posts_nothing(self):
        other_team = TeamMaster.objects.create(team_name="Other")
        channel, _, task = self.thread_and_task()
        Channel.objects.filter(pk=channel.pk).update(team=other_team)
        self.assertEqual(self.closed_in_nowhere(task), [])

    def test_a_deleted_task_posts_nothing(self):
        channel, _, task = self.thread_and_task()
        task.is_deleted = True
        task.status = "Closed"
        task.save()
        self.assertEqual(self.notices(channel), [])

    def test_a_task_created_already_closed_posts_nothing(self):
        """Creation is not a close: only the transition is news."""
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user2, "Asked", at=self.ago(hours=2))
        self.make_task(
            "Born closed", status="Closed", chat_id=str(channel.id), thread_id=str(root.id)
        )
        self.assertEqual(self.notices(channel), [])


class TestItNeverBreaksTheClose(CloseNoticeBase):
    def test_a_failing_write_is_logged_and_the_task_still_closes(self):
        channel, _, task = self.thread_and_task()
        with mock.patch(
            "origin.views.chat.message_views._allocate_seq_and_create_message",
            side_effect=RuntimeError("boom"),
        ):
            self.close(task)
        task.refresh_from_db()
        self.assertEqual(task.status, "Closed")
        self.assertTrue(TaskActivity.objects.filter(task=task, new_value="Closed").exists())
        self.assertEqual(self.notices(channel), [])

    def test_a_database_error_inside_the_funnel_leaves_the_close_standing(self):
        """The funnel's own savepoint rolls the line back; the close's transaction stays usable."""
        channel, _, task = self.thread_and_task()
        with mock.patch.object(Message.objects, "create", side_effect=IntegrityError("seq")):
            self.close(task)
        task.refresh_from_db()  # a query after the failure: the transaction was not poisoned
        self.assertEqual(task.status, "Closed")
        self.assertEqual(self.notices(channel), [])

    def test_a_rolled_back_close_posts_nothing(self):
        channel, _, task = self.thread_and_task()
        try:
            with transaction.atomic():
                self.close(task)
                self.assertEqual(len(self.notices(channel)), 1)
                raise RuntimeError("the request failed after the close")
        except RuntimeError:
            pass
        self.assertEqual(self.notices(channel), [])
        task.refresh_from_db()
        self.assertEqual(task.status, "Open")


class TestWhoReadsIt(CloseNoticeBase):
    def test_the_client_who_started_the_thread_reads_it_in_her_briefing(self):
        """User2 is the client; the member of `self.ctx` (user) is the PM. As the client."""
        channel, _, task = self.thread_and_task()
        self.close(task)
        from origin.search_engine.agent.tools import ToolContext
        from origin.services import briefing

        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        out = briefing.build_briefing(ctx, now=timezone.now())
        text = json.dumps(out["changed_for_me"])
        self.assertIn(f"Task {task.display_id} is now Closed.", text)


class TestFlagOffIsMain(BriefingBase):
    """`AGENT_CLOSING_LINE_IN_ORIGIN_THREAD` off (the default): closing a task posts
    nothing, and what the in-app agent reads in the thread is what `origin/main` serves.

    PIN. `GOLDEN` was recorded on pristine `origin/main` (346816f2: no flag, no notice
    service) with the same test body, by copying this class into a clean worktree:

        git -C <genos-api> worktree add --detach <wt> origin/main
        # copy this file's `TestFlagOffIsMain` + its imports into <wt>/origin/tests/ as a module
        RECORD_GOLDEN=1 python manage.py test <module>.TestFlagOffIsMain --noinput
        # prints `GOLDEN = "<sha256>"`

    The hash is over `fetch_chat_thread`'s result for the thread after the close (ids and
    times replaced by placeholders), the thread's message rows and its reply count.
    """

    GOLDEN = "21192f28aee0f436195f6f96f695a8e14486b85901e71ae9a0deff90574c7f0d"

    def snapshot(self):
        from origin.search_engine.agent.tools import REGISTRY, ToolContext

        channel = self.channel([self.user, self.user2], kind=ChannelKind.GM)
        root = self.message(channel, self.user2, "Please fix Y", at=self.ago(hours=9))
        self.message(channel, self.user, "On it", root=root, at=self.ago(hours=8))
        task = self.make_task(
            TITLE,
            assignee=self.user,
            reporter=self.user,
            chat_id=str(channel.id),
            thread_id=str(root.id),
        )
        task.status = "Closed"
        task.save()
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        out = REGISTRY["fetch_chat_thread"].run(
            {"chat_id": str(channel.id), "thread_id": str(root.id)}, ctx
        )
        rows = list(
            Message.objects.filter(channel=channel)
            .order_by("seq")
            .values_list("body_text", "is_thread_reply", "sender_id", "reply_count")
        )
        return channel, root, out, rows

    @staticmethod
    def normalised(value):
        text = json.dumps(value, sort_keys=True, default=str)
        text = re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", text
        )
        text = re.sub(r"\d{4}-\d{2}-\d{2}T[0-9:.+\-Z]+", "<time>", text)
        return re.sub(r'"(sender_id|user_id)": \d+', r'"\1": <id>', text)

    def test_nothing_is_posted_and_the_thread_reads_as_on_main(self):
        channel, _root, out, rows = self.snapshot()
        self.assertEqual(Message.objects.filter(metadata__systemNotice="task_closed").count(), 0)
        blob = self.normalised([out, [list(r) for r in rows]])
        digest = hashlib.sha256(blob.encode()).hexdigest()
        if os.environ.get("RECORD_GOLDEN"):
            print(f'GOLDEN = "{digest}"')
            print("BLOB", blob)
            return
        self.assertEqual(digest, self.GOLDEN)

    def test_with_the_flag_on_the_thread_differs_by_exactly_the_line(self):
        with override_settings(
            SEARCH_ENGINE={**settings.SEARCH_ENGINE, "AGENT_CLOSING_LINE_IN_ORIGIN_THREAD": True}
        ):
            _channel, _root, out, rows = self.snapshot()
        self.assertEqual(Message.objects.filter(metadata__systemNotice="task_closed").count(), 1)
        # root, the reply, then the line: nothing else in the thread differs.
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            rows[2][0],
            f"Task {Message.objects.get(metadata__systemNotice='task_closed').metadata['closedTaskDisplayId']} is now Closed.",
        )
        self.assertIsNone(rows[2][2])  # no sender
        self.assertIn("is now Closed.", json.dumps(out))
        self.assertNotEqual(
            hashlib.sha256(self.normalised([out, [list(r) for r in rows]]).encode()).hexdigest(),
            self.GOLDEN,
        )
