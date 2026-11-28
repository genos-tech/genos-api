"""Moving a milestone costs the same number of queries whatever it holds (MIP-2539).

It cost about 12 per task (the sub-tree walked task by task, numbers claimed one by one, one history
insert per task, a card moved per task). These pin the count inside the move's transaction and what
the batched writes leave behind. The after-commit card work (the rebuild and the pushes, MIP-2561:
it was about 20 queries per card) is pinned too.
"""

from unittest import mock

from django.db import IntegrityError, connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageMention,
    ReadCursor,
)
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskDependency, TaskMaster, TaskNumberAlias
from origin.services import realtime_push, task_card, task_card_move
from origin.services.milestone_manage import update_milestone
from origin.services.task_project_move import milestone_subtree_task_ids, relocate_tasks

from .test_automation_milestone_rules import MilestoneRuleBase


class MoveBase(MilestoneRuleBase):
    def chat(self, project):
        return Channel.objects.get(kind=ChannelKind.PM, legacy_chat_id=project.pk)

    def card(self, task, replies=1):
        channel = self.chat(self.a)
        seq = Message.objects.filter(channel=channel).count() + 1000
        card = Message.objects.create(
            channel=channel,
            task_id=task.task_id,
            seq=seq,
            sender=self.user,
            body=[],
            metadata={"taskId": task.task_id},
            reply_count=replies,
        )
        for n in range(replies):
            Message.objects.create(
                channel=channel,
                task_id=task.task_id,
                seq=seq + 1 + n,
                sender=self.user,
                body=[],
                is_thread_reply=True,
                parent=card,
                thread_root=card,
            )
        return card

    def loaded_milestone(self, tasks):
        """A milestone whose tasks have a sub-task, a card with a reply, a note, a collaborator,
        a tag and a dependency on the next one."""
        milestone = self.milestone(tasks=tasks, tags=self.tags("tag-a"))
        for task in self.members:
            self.task(title="sub", parent_task_id=task.task_id)
            self.card(task)
            TaskNoteMaster.objects.create(
                team=self.team, project=self.a, task=task, title="n", body=[]
            )
            task.collaborators.add(self.user2)
        for first, second in zip(self.members, self.members[1:]):
            TaskDependency.objects.create(team=self.team, blocker_task=first, blocked_task=second)
        TaskMaster.objects.filter(pk__in=[t.pk for t in self.members]).update(
            tags=self.tags("tag-a")
        )
        return milestone

    def move_queries(self, milestone):
        with self.captureOnCommitCallbacks():
            with CaptureQueriesContext(connection) as captured:
                update_milestone(milestone, fields={}, project_move=self.x, report={})
        return len(captured)


class MilestoneMoveQueryCountTests(MoveBase):
    def test_plain_tasks_do_not_add_queries(self):
        small = self.move_queries(self.milestone(tasks=3))
        large = self.move_queries(self.milestone(tasks=20))
        self.assertEqual(small, large)
        self.assertLessEqual(large, 50)

    def test_tasks_with_cards_notes_and_edges_do_not_add_queries(self):
        small = self.move_queries(self.loaded_milestone(3))
        large = self.move_queries(self.loaded_milestone(12))
        self.assertEqual(small, large)
        self.assertLessEqual(large, 90)


class MilestoneMoveBatchTests(MoveBase):
    def numbers(self, tasks):
        return list(
            TaskMaster.objects.filter(pk__in=[t.pk for t in tasks])
            .order_by("task_id")
            .values_list("project_task_number", flat=True)
        )

    def move(self, tasks, project):
        with self.captureOnCommitCallbacks(execute=True):
            relocate_tasks([t.pk for t in tasks], project_id=project.pk, team_id=project.team_id)

    def test_the_numbers_follow_task_order_and_a_task_moving_back_keeps_its_old_one(self):
        self.task(self.x)  # XX-1
        one, two = self.task(), self.task()
        before = self.numbers([one, two])
        self.move([one, two], self.x)
        self.assertEqual(self.numbers([one, two]), [2, 3])
        newcomer = self.task(self.x)  # XX-4
        self.move([one, two, newcomer], self.a)
        self.assertEqual(self.numbers([one, two]), before)
        self.assertEqual(self.numbers([newcomer])[0], before[-1] + 1)
        # The alias a task takes back goes; the ones for XX stay.
        self.assertFalse(TaskNumberAlias.objects.filter(project=self.a).exists())
        self.assertEqual(TaskNumberAlias.objects.filter(project=self.x).count(), 3)
        self.move([one, two], self.x)
        self.assertEqual(self.numbers([one, two]), [2, 3])

    def test_a_lost_number_race_claims_one_by_one(self):
        self.task(self.x)
        one, two = self.task(), self.task()
        with mock.patch.object(
            TaskMaster.objects, "bulk_update", side_effect=IntegrityError("taken")
        ):
            self.move([one, two], self.x)
        self.assertEqual(sorted(self.numbers([one, two])), [2, 3])

    def test_each_moved_task_has_its_history_row_and_webhook_event(self):
        milestone = self.milestone(tasks=3)
        ids = milestone_subtree_task_ids(milestone)
        TaskActivity.objects.all().delete()
        # The setup's own events never flush in a test, and a task with one pending adds no other.
        connection._genos_webhook_pending = {}
        with mock.patch("origin.services.webhook_enqueue.enqueue_event") as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                update_milestone(milestone, fields={}, project_move=self.x)
        moved = TaskActivity.objects.filter(field_name="project_id")
        self.assertEqual(
            sorted(a.task_id for a in moved if (a.metadata or {}).get("cascade") == "project_move"),
            sorted(ids),
        )
        events = {call.args[2]["id"]: call.args[2] for call in enqueue.call_args_list}
        self.assertEqual(set(events), ids)
        for task in TaskMaster.objects.filter(pk__in=ids).select_related("project"):
            self.assertEqual(events[task.pk]["display_id"], task.display_id)

    def test_several_cards_arrive_in_order_with_their_threads(self):
        milestone = self.milestone(tasks=3)
        cards = [self.card(task, replies=2) for task in self.members]
        source, destination = self.chat(self.a), self.chat(self.x)
        made_from = self.task(title="from a thread", chat_id=str(source.pk))
        TaskMaster.objects.filter(pk=made_from.pk).update(thread_id=str(cards[1].pk))
        # The member last read the second card: everything from the first one up leaves.
        cursor = ReadCursor.objects.create(
            channel=source, user=self.user2, last_read_message=cards[1]
        )

        with self.captureOnCommitCallbacks(execute=True):
            update_milestone(milestone, fields={}, project_move=self.x)

        for card in cards:
            thread = list(Message.objects.filter(thread_root=card).order_by("seq"))
            card.refresh_from_db()
            self.assertEqual(card.channel_id, destination.pk)
            self.assertEqual([m.channel_id for m in thread], [destination.pk] * 2)
            self.assertEqual([m.seq for m in thread], [card.seq + 1, card.seq + 2])
        self.assertEqual([c.seq for c in cards], sorted(c.seq for c in cards))
        made_from.refresh_from_db()
        self.assertEqual(
            (made_from.chat_id, made_from.thread_id), (str(destination.pk), str(cards[1].pk))
        )
        cursor.refresh_from_db()
        self.assertEqual(cursor.channel_id, source.pk)
        self.assertNotIn(
            cursor.last_read_message_id, [m.pk for m in Message.objects.filter(channel=destination)]
        )


@override_settings(SOCKETS_INTERNAL_URL="http://sockets", SOCKETS_INTERNAL_SECRET="secret")
class MilestoneMoveCardRebuildTests(MoveBase):
    """The cards are rebuilt and pushed together after the commit (MIP-2561)."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(realtime_push, "_post", return_value=True)
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def move_with_cards(self, tasks):
        """Moves a milestone of `tasks` carded, assigned tasks. Returns the cards and the
        queries the cards cost after the commit."""
        milestone = self.milestone(tasks=tasks)
        members = list(self.members)
        TaskMaster.objects.filter(pk__in=[t.pk for t in members]).update(assignee=self.user)
        cards = [self.card(task) for task in members]
        self.post.reset_mock()
        queries: list = []
        announce = task_card_move._announce

        def counted(rows):
            with CaptureQueriesContext(connection) as captured:
                announce(rows)
            queries.append(len(captured))

        with mock.patch.object(task_card_move, "_announce", side_effect=counted):
            with self.captureOnCommitCallbacks(execute=True):
                update_milestone(milestone, fields={}, project_move=self.x)
        self.assertEqual(len(queries), 1)
        return cards, queries[0]

    def pushed(self):
        """The ids of the messages pushed, in order."""
        return [
            call.args[0]["message"]["id"]
            for call in self.post.call_args_list
            if call.args[1] == realtime_push.MESSAGE_PATH
        ]

    def test_the_queries_do_not_grow_with_the_cards(self):
        _, small = self.move_with_cards(3)
        _, large = self.move_with_cards(25)
        self.assertEqual(small, large)
        self.assertLessEqual(large, 18)

    def test_each_card_is_rewritten_mentions_and_all_and_pushed_in_order(self):
        cards, _ = self.move_with_cards(3)
        member = ChannelMember.objects.filter(channel=self.chat(self.x), user=self.user)
        self.assertTrue(member.exists())
        expected = []
        for card in cards:
            card.refresh_from_db()
            task = TaskMaster.objects.get(pk=card.task_id)
            self.assertEqual(card.metadata["displayId"], task.display_id)
            self.assertEqual(card.body_text, task.title)
            self.assertIsNotNone(card.edited_at)
            self.assertEqual(
                list(
                    MessageMention.objects.filter(message=card).values_list(
                        "mentioned_user_id", flat=True
                    )
                ),
                [self.user.pk],
            )
            reply = Message.objects.get(thread_root=card)
            # For the rewrite, for the move, then its thread.
            expected += [str(card.pk), str(card.pk), str(reply.pk)]
        self.assertEqual(self.pushed(), expected)

    def test_a_card_that_cannot_be_built_does_not_stop_the_others(self):
        build = task_card.task_card_body

        def fail_one(**kwargs):
            if kwargs["title"] == "t1":
                raise ValueError("broken")
            return build(**kwargs)

        with mock.patch.object(task_card, "task_card_body", side_effect=fail_one):
            with self.assertLogs("origin.services.task_card_post", "WARNING"):
                cards, _ = self.move_with_cards(3)
        rewritten = [Message.objects.get(pk=card.pk).edited_at is not None for card in cards]
        self.assertEqual(rewritten.count(False), 1)

    def test_a_failed_batch_write_falls_back_to_one_card_at_a_time(self):
        with mock.patch(
            "origin.views.chat.message_views.rewrite_card_messages",
            side_effect=IntegrityError("boom"),
        ):
            with self.assertLogs("origin.services.task_card_post", "WARNING"):
                cards, _ = self.move_with_cards(3)
        for card in cards:
            card.refresh_from_db()
            self.assertIsNotNone(card.edited_at)
        self.assertEqual(self.pushed().count(str(cards[0].pk)), 2)

    def test_a_failing_push_stays_after_the_commit(self):
        self.post.side_effect = ValueError("boom")
        with self.assertLogs("django.test", level="ERROR"):
            cards, _ = self.move_with_cards(2)
        self.assertEqual(Message.objects.get(pk=cards[0].pk).channel_id, self.chat(self.x).pk)
