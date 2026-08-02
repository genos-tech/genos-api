"""What actually queues a webhook.

Three properties, each of which would be a support ticket if wrong:

  * **One event per save, never one per changed field.** Editing four
    columns is one thing happening.
  * **Only subscribers of that event, only in that team.** A webhook is
    a standing instruction scoped to a team; leaking across teams would
    be the worst possible bug in this feature.
  * **A failing enqueue never breaks the task write.** An integration is
    an observer; an observer that can fail the observed action is a
    liability.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster
from origin.models.common.webhook_models import (
    EVENT_TASK_COMPLETED,
    EVENT_TASK_CREATED,
    EVENT_TASK_UPDATED,
    WebhookDelivery,
    WebhookEndpoint,
    generate_secret,
)
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class WebhookEmitBase(BaseAPITestCase):
    """`TestCase` wraps each test in a transaction that never commits, so
    `transaction.on_commit` callbacks would never run. `captureOnCommitCallbacks`
    is the supported way to force them — without it every assertion here
    would pass vacuously."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Hooked", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def _endpoint(self, events, team=None):
        e = WebhookEndpoint(team=team or self.team, url="https://example.com/hook", events=events)
        e.set_secret(generate_secret())
        e.save()
        return e

    def _make_task(self, **overrides):
        with self.captureOnCommitCallbacks(execute=True):
            return TaskMaster.objects.create(
                team=self.team,
                project=self.project,
                title=overrides.pop("title", "Hook me"),
                status=overrides.pop("status", "Open"),
                reporter=self.user,
                **overrides,
            )


class TestTaskEvents(WebhookEmitBase):
    def test_creating_a_task_queues_task_created(self):
        self._endpoint([EVENT_TASK_CREATED])
        self._make_task()
        deliveries = WebhookDelivery.objects.all()
        self.assertEqual(deliveries.count(), 1)
        self.assertEqual(deliveries.first().event, EVENT_TASK_CREATED)

    def test_the_payload_matches_the_public_api_shape(self):
        """An integrator reading /api/public/v1/tasks/ and one receiving
        task.created should be looking at the same object."""
        self._endpoint([EVENT_TASK_CREATED])
        task = self._make_task(title="Shaped")
        payload = WebhookDelivery.objects.first().payload
        self.assertEqual(payload["id"], task.task_id)
        self.assertEqual(payload["title"], "Shaped")
        self.assertEqual(str(payload["team_id"]), str(self.team.team_id))
        self.assertIn("display_id", payload)

    def test_updating_queues_task_updated(self):
        self._endpoint([EVENT_TASK_UPDATED])
        task = self._make_task()
        with self.captureOnCommitCallbacks(execute=True):
            task.title = "Renamed"
            task.save()
        self.assertEqual(WebhookDelivery.objects.count(), 1)
        self.assertEqual(WebhookDelivery.objects.first().event, EVENT_TASK_UPDATED)

    def test_closing_queues_task_completed_not_updated(self):
        self._endpoint([EVENT_TASK_COMPLETED, EVENT_TASK_UPDATED])
        task = self._make_task()
        with self.captureOnCommitCallbacks(execute=True):
            task.status = "Closed"
            task.save()
        events = [d.event for d in WebhookDelivery.objects.all()]
        self.assertEqual(events, [EVENT_TASK_COMPLETED])

    def test_one_event_per_save_not_one_per_field(self):
        """Editing four columns is one thing happening."""
        self._endpoint([EVENT_TASK_UPDATED])
        task = self._make_task()
        with self.captureOnCommitCallbacks(execute=True):
            task.title = "A"
            task.priority = "High"
            task.save()
        self.assertEqual(WebhookDelivery.objects.count(), 1)

    def test_a_placeholder_draft_emits_nothing(self):
        """`is_init_task` rows are the empty create form; an integrator
        must never see a task that does not exist yet."""
        self._endpoint([EVENT_TASK_CREATED])
        with self.captureOnCommitCallbacks(execute=True):
            TaskMaster.objects.create(
                team=self.team,
                project=self.project,
                title="",
                status="Open",
                is_init_task=True,
            )
        self.assertEqual(WebhookDelivery.objects.count(), 0)


class TestSubscriptionScoping(WebhookEmitBase):
    def test_only_subscribers_of_that_event_are_queued(self):
        self._endpoint([EVENT_TASK_COMPLETED])  # not subscribed to created
        self._make_task()
        self.assertEqual(WebhookDelivery.objects.count(), 0)

    def test_another_teams_endpoint_is_never_queued(self):
        other_owner = User.objects.create_user(
            username="hookother", email="hookother@example.com", password="pw"
        )
        other_team = TeamMaster.objects.create(
            team_name="Hook Other", team_email="hookother@example.com", owner=other_owner
        )
        self._endpoint([EVENT_TASK_CREATED], team=other_team)
        self._make_task()
        self.assertEqual(WebhookDelivery.objects.count(), 0)

    def test_an_inactive_endpoint_is_not_queued(self):
        e = self._endpoint([EVENT_TASK_CREATED])
        WebhookEndpoint.objects.filter(pk=e.pk).update(is_active=False)
        self._make_task()
        self.assertEqual(WebhookDelivery.objects.count(), 0)

    def test_every_subscriber_gets_its_own_delivery(self):
        self._endpoint([EVENT_TASK_CREATED])
        self._endpoint([EVENT_TASK_CREATED])
        self._make_task()
        self.assertEqual(WebhookDelivery.objects.count(), 2)


class TestEnqueueIsBestEffort(WebhookEmitBase):
    def test_a_broken_enqueue_does_not_break_the_task_write(self):
        self._endpoint([EVENT_TASK_CREATED])
        with patch(
            "origin.services.webhook_enqueue.WebhookDelivery.objects.bulk_create",
            side_effect=RuntimeError("boom"),
        ):
            task = self._make_task(title="Survives")
        self.assertTrue(TaskMaster.objects.filter(task_id=task.task_id).exists())
        self.assertEqual(WebhookDelivery.objects.count(), 0)

    def test_nothing_is_queued_before_the_transaction_commits(self):
        """A webhook body carries the task's fields; firing inside the
        transaction could deliver a state that then rolls back."""
        self._endpoint([EVENT_TASK_CREATED])
        with self.captureOnCommitCallbacks(execute=False):
            TaskMaster.objects.create(
                team=self.team, project=self.project, title="Uncommitted", status="Open"
            )
            self.assertEqual(WebhookDelivery.objects.count(), 0)
