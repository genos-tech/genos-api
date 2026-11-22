"""`comment_id` allocation (MIP-1954): MAX + 1 under a task-row lock, never COUNT + 1.

Removing a row (a purge of old deleted comments, a force-delete) must not make the next comment
re-use the id of a surviving one.
"""

import threading

from django.contrib.auth import get_user_model
from django.db import connection, transaction

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services.task_comment_ids import next_task_comment_id
from origin.tests.test_base import BaseAPITestCase
from origin.tests.transaction_support import CheapFlushTransactionTestCase

User = get_user_model()


class TaskCommentIdAllocatorTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="IdProj", owner=self.user, project_system_user=self.user
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open"
        )

    def _body(self, text):
        return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]

    def _post(self, text):
        return self.client.post(
            "/api/v2/task/comment/",
            {"task_id": self.task.task_id, "comment_body": self._body(text)},
            format="json",
        )

    def test_rest_ids_stay_unique_after_a_middle_row_is_removed(self):
        for t in ("a", "b", "c"):
            self.assertEqual(self._post(t).status_code, 201)
        TaskComments.objects.filter(task=self.task, comment_id=2).delete()
        self.assertEqual(self._post("d").status_code, 201)
        self.assertEqual(self._post("e").status_code, 201)
        ids = list(
            TaskComments.objects.filter(task=self.task)
            .order_by("comment_id")
            .values_list("comment_id", flat=True)
        )
        self.assertEqual(ids, [1, 3, 4, 5])

    def test_agent_tool_and_rest_share_the_sequence(self):
        self._post("a")
        self._post("b")
        TaskComments.objects.filter(task=self.task, comment_id=1).delete()
        with transaction.atomic():
            self.assertEqual(next_task_comment_id(self.task.task_id), 3)

    def test_first_comment_is_one_and_deleted_rows_keep_their_slot(self):
        with transaction.atomic():
            self.assertEqual(next_task_comment_id(self.task.task_id), 1)
        self._post("a")
        TaskComments.objects.filter(task=self.task).update(is_deleted=True)
        with transaction.atomic():
            self.assertEqual(next_task_comment_id(self.task.task_id), 2)

    def test_outside_a_transaction_is_refused(self):
        # BaseAPITestCase wraps tests in a transaction, so check the guard directly.
        with self.assertRaises(RuntimeError):
            from unittest import mock

            with mock.patch("origin.services.task_comment_ids.transaction.get_connection") as gc:
                gc.return_value.in_atomic_block = False
                next_task_comment_id(self.task.task_id)


class TaskCommentIdConcurrencyTests(CheapFlushTransactionTestCase):
    """Real commits from two threads, so not inside the usual wrapping transaction."""

    def test_concurrent_adds_never_collide(self):
        user = User.objects.create_user(username="cu", email="cu@example.com", password="x")
        team = TeamMaster.objects.create(team_name="CT", team_email="ct@example.com", owner=user)
        project = ProjectMaster.objects.create(
            team=team, project_name="CP", owner=user, project_system_user=user
        )
        task = TaskMaster.objects.create(team=team, project=project, title="T", status="Open")
        errors, got = [], []
        barrier = threading.Barrier(4)

        def add():
            try:
                barrier.wait()
                with transaction.atomic():
                    n = next_task_comment_id(task.task_id)
                    TaskComments.objects.create(
                        task=task, sender=user, comment_id=n, comment_body=[]
                    )
                got.append(n)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                connection.close()

        threads = [threading.Thread(target=add) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(sorted(got), [1, 2, 3, 4])
