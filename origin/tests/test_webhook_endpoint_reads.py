"""The team's webhook endpoints are read once per commit, not once per task (MIP-2573).

A move or a bulk write flushes one `on_commit` callback per task, and each read the endpoints.
What is delivered does not change, and what one commit read is not carried into the next.
"""

from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from origin.models.common.webhook_models import (
    EVENT_TASK_CREATED,
    EVENT_TASK_UPDATED,
    WebhookDelivery,
    WebhookEndpoint,
)
from origin.models.task.task_models import TaskMaster
from origin.services.milestone_manage import update_milestone

from .test_milestone_move_queries import MoveBase
from .test_webhook_emit import WebhookEmitBase

ENDPOINT_TABLE = WebhookEndpoint._meta.db_table


def _reads(queries) -> list:
    return [q["sql"] for q in queries if f'FROM "{ENDPOINT_TABLE}"' in q["sql"]]


class MoveEndpointReadTests(MoveBase):
    def after_commit_queries(self, tasks):
        """The queries of the callbacks of a move of a milestone of `tasks` tasks."""
        milestone = self.milestone(tasks=tasks)
        # The setup's own events never flush in a test, and a task with one pending adds no other.
        connection._genos_webhook_pending = {}
        with self.captureOnCommitCallbacks() as callbacks:
            update_milestone(milestone, fields={}, project_move=self.x)
        with CaptureQueriesContext(connection) as captured:
            for callback in callbacks:
                callback()
        return captured.captured_queries

    def test_the_after_commit_queries_do_not_grow_with_the_tasks(self):
        # The first move warms what is only read once per process.
        self.after_commit_queries(2)
        small = self.after_commit_queries(3)
        large = self.after_commit_queries(20)
        self.assertEqual(len(_reads(small)), 1, _reads(small))
        self.assertEqual(len(_reads(large)), 1, _reads(large))
        self.assertEqual(len(small), len(large))


class CommitBoundaryTests(WebhookEmitBase):
    def tasks(self, n):
        return [
            TaskMaster.objects.create(
                team=self.team, project=self.project, title=f"t{i}", status="Open"
            )
            for i in range(n)
        ]

    def committed(self, write):
        """Runs `write` and its callbacks; returns the endpoint reads of the callbacks."""
        connection._genos_webhook_pending = {}
        with self.captureOnCommitCallbacks() as callbacks:
            write()
        with CaptureQueriesContext(connection) as captured:
            for callback in callbacks:
                callback()
        return _reads(captured.captured_queries)

    def test_every_task_is_delivered_after_one_read(self):
        self._endpoint([EVENT_TASK_CREATED])
        self._endpoint([EVENT_TASK_CREATED])
        self.assertEqual(len(self.committed(lambda: self.tasks(4))), 1)
        self.assertEqual(WebhookDelivery.objects.count(), 8)

    def test_the_next_commit_sees_an_endpoint_added_and_switched_off_since(self):
        first = self._endpoint([EVENT_TASK_CREATED])
        self.committed(lambda: self.tasks(2))
        first.is_active = False
        first.save()
        second = self._endpoint([EVENT_TASK_CREATED])
        WebhookDelivery.objects.all().delete()
        self.assertEqual(len(self.committed(lambda: self.tasks(2))), 1)
        self.assertEqual(
            list(WebhookDelivery.objects.values_list("endpoint_id", flat=True)), [second.pk] * 2
        )

    def test_the_next_commit_sees_a_changed_filter(self):
        endpoint = self._endpoint([EVENT_TASK_CREATED])
        self.committed(lambda: self.tasks(1))
        endpoint.events = [EVENT_TASK_UPDATED]
        endpoint.save()
        WebhookDelivery.objects.all().delete()
        self.committed(lambda: self.tasks(1))
        self.assertEqual(WebhookDelivery.objects.count(), 0)

    def test_a_rolled_back_transaction_leaves_nothing_read(self):
        self._endpoint([EVENT_TASK_CREATED])

        def rolled_back():
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    self.tasks(1)
                    raise RuntimeError("rollback")

        self.committed(rolled_back)
        late = self._endpoint([EVENT_TASK_CREATED])
        self.assertEqual(len(self.committed(lambda: self.tasks(1))), 1)
        self.assertTrue(WebhookDelivery.objects.filter(endpoint=late).exists())
