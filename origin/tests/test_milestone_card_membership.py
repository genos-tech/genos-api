"""A milestone's open chat card hears that tasks joined or left it (MIP-2570).

The card reads its progress live and its stored body does not hold it, so the server sends the card
again, unchanged, for the open chat to read it again.
"""

from unittest import mock

from django.db import transaction

from origin.models.chat.unified_models import Message
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.manage_milestone import MANAGE_MILESTONE
from origin.services import realtime_push, task_card_post
from origin.services.activity_suppression import suppress_task_activity
from origin.signals.task_signals import update_tasks_recorded

from .test_task_card_post import CardPostBase


class MilestoneCardMembershipTests(CardPostBase):
    def setUp(self):
        super().setUp()
        self.first, self.first_card = self._milestone("First")
        self.second, self.second_card = self._milestone("Second")
        self.task = self._create()
        patcher = mock.patch.object(realtime_push, "push_message")
        self.pushed = patcher.start()
        self.addCleanup(patcher.stop)

    def _milestone(self, title):
        with self.captureOnCommitCallbacks(execute=True):
            made = MANAGE_MILESTONE.run(
                {"action": "create", "project_id": self.project.project_id, "title": title},
                self.ctx,
            )
        milestone = MilestoneMaster.objects.get(milestone_id=made["milestone_id"])
        card = Message.objects.get(
            channel=self.channel, task_id=milestone.task_id, parent_id__isnull=True
        )
        return milestone, card

    def _announced(self) -> list:
        """The milestone cards sent, in order (task cards left out)."""
        cards = {self.first_card.pk: "first", self.second_card.pk: "second"}
        return [
            cards[c.args[0].pk]
            for c in self.pushed.call_args_list
            if c.args[0].pk in cards and c.args[1] == realtime_push.UPDATED
        ]

    def _write(self, write):
        self.pushed.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            write()
        return sorted(self._announced())

    def _save(self, **fields):
        def write():
            task = TaskMaster.objects.get(pk=self.task.pk)
            for name, value in fields.items():
                setattr(task, name, value)
            task.save()

        return self._write(write)

    def test_moving_a_task_into_a_milestone_announces_it(self):
        self.assertEqual(self._save(milestone=self.first), ["first"])

    def test_moving_a_task_out_announces_the_milestone_it_left(self):
        self._save(milestone=self.first)

        self.assertEqual(self._save(milestone=None), ["first"])

    def test_moving_between_milestones_announces_both(self):
        self._save(milestone=self.first)

        self.assertEqual(self._save(milestone=self.second), ["first", "second"])

    def test_an_app_move_is_announced(self):
        self.authenticate()

        def put():
            res = self.client.put(
                "/api/v2/task/",
                {"task_id": self.task.task_id, "milestone": self.first.milestone_id},
                format="json",
            )
            self.assertEqual(res.status_code, 200, res.data)

        self.assertEqual(self._write(put), ["first"])

    def test_membership_by_sub_task_counts_both_ways(self):
        self.assertEqual(self._save(parent_task_id=self.first.task_id), ["first"])
        self.assertEqual(self._save(parent_task_id=None), ["first"])

    def test_an_ordinary_parent_announces_nothing(self):
        parent = self._create(title="Parent")

        self.assertEqual(self._save(parent_task_id=parent.task_id), [])

    def test_deleting_and_restoring_a_task_announces_its_milestone(self):
        self._save(milestone=self.first)

        self.assertEqual(self._save(status="Deleted"), ["first"])
        self.assertEqual(self._save(status="Open"), ["first"])
        self.assertEqual(self._save(is_deleted=True), ["first"])

    def test_a_hard_delete_announces_the_milestone(self):
        self._save(milestone=self.first)

        self.assertEqual(
            self._write(lambda: TaskMaster.objects.get(pk=self.task.pk).delete()), ["first"]
        )

    def test_an_edit_that_keeps_the_membership_announces_nothing(self):
        self._save(milestone=self.first)

        self.assertEqual(self._save(priority="High"), [])

    def test_a_bulk_move_announces_each_milestone_once(self):
        others = [self._create(title=f"Task {n}") for n in range(3)]
        ids = [self.task.pk, *(t.pk for t in others)]
        rows = TaskMaster.objects.filter(pk__in=ids)
        rows.update(milestone=self.first)

        with mock.patch.object(
            task_card_post,
            "refresh_milestone_card",
            wraps=task_card_post.refresh_milestone_card,
        ) as refreshed:
            sent = self._write(
                lambda: update_tasks_recorded(rows, cascade="test", milestone=self.second)
            )

        self.assertEqual(sent, ["first", "second"])
        self.assertEqual(refreshed.call_count, 2)

    def test_many_saves_in_one_transaction_announce_once(self):
        others = [self._create(title=f"Task {n}") for n in range(3)]

        def write():
            with transaction.atomic():
                for task in TaskMaster.objects.filter(pk__in=[t.pk for t in others]):
                    task.milestone = self.first
                    task.save()

        self.assertEqual(self._write(write), ["first"])

    def test_a_rolled_back_savepoint_does_not_lose_the_next_move(self):
        def write():
            try:
                with transaction.atomic():
                    task = TaskMaster.objects.get(pk=self.task.pk)
                    task.milestone = self.second
                    task.save()
                    raise RuntimeError("undo")
            except RuntimeError:
                pass
            task = TaskMaster.objects.get(pk=self.task.pk)
            task.milestone = self.first
            task.save()

        self.assertIn("first", self._write(write))

    def test_bulk_teardown_announces_nothing(self):
        def write():
            with suppress_task_activity():
                task = TaskMaster.objects.get(pk=self.task.pk)
                task.milestone = self.first
                task.save()

        self.assertEqual(self._write(write), [])
