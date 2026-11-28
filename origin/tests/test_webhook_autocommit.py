"""Task webhooks for saves outside a transaction (MIP-2586).

Every other webhook test is a `TestCase`, which is always inside a transaction; the app's task
edit is not, so these commit for real. One user action is one delivery per endpoint.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster, TeamMembers
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
from origin.services import webhook_enqueue
from origin.tests.transaction_support import CheapFlushTransactionTestCase

User = get_user_model()

TASK_URL = "/api/v2/task/"
ALL_EVENTS = [EVENT_TASK_CREATED, EVENT_TASK_UPDATED, EVENT_TASK_COMPLETED]


class _Base(CheapFlushTransactionTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username="hook2586", email="hook2586@example.com", password="x"
        )
        self.team = TeamMaster.objects.create(
            team_name="Hook 2586", team_email="hook2586@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user)
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Hooked", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        endpoint = WebhookEndpoint(team=self.team, url="https://example.com/h", events=ALL_EVENTS)
        endpoint.set_secret(generate_secret())
        endpoint.save()
        self.client = APIClient()
        token = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        # Indexing and GitHub registration are not what these tests are about.
        patcher = mock.patch("origin.views.task.task_views.ensure_webhooks_for_links")
        patcher.start()
        self.addCleanup(patcher.stop)

    def events(self):
        return list(WebhookDelivery.objects.order_by("pk").values_list("event", flat=True))

    def clear(self):
        WebhookDelivery.objects.all().delete()

    def task(self, **fields):
        task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="A task",
            status="Open",
            reporter=self.user,
            **fields,
        )
        self.clear()
        return task

    def body(self, **overrides):
        return {
            "team": str(self.team.team_id),
            "project": self.project.project_id,
            "assignee": str(self.user.id),
            "reporter": str(self.user.id),
            "title": "Made in the app",
            "priority": "Normal",
            "effort_level": "Low",
            "status": "Open",
            "content": None,
            "due_date": None,
            "links": [],
            "tags": [],
            "is_init_task": False,
            **overrides,
        }


class RestEditTests(_Base):
    def put(self, task, **fields):
        response = self.client.put(TASK_URL, {"task_id": task.task_id, **fields}, format="json")
        self.assertEqual(response.status_code, 200, response.content)

    def test_title_status_and_completion_each_queue_one_delivery(self):
        task = self.task()
        for fields, event in (
            ({"title": "Renamed"}, EVENT_TASK_UPDATED),
            ({"status": "WIP"}, EVENT_TASK_UPDATED),
            ({"status": "Closed"}, EVENT_TASK_COMPLETED),
        ):
            with self.subTest(fields=fields):
                self.put(task, **fields)
                self.assertEqual(self.events(), [event])
                self.clear()

    def test_a_save_that_writes_the_row_twice_queues_one_delivery(self):
        task = self.task(due_date="2026-06-30")
        self.put(task, title="Renamed", due_date=None)
        self.assertEqual(self.events(), [EVENT_TASK_UPDATED])
        self.assertIsNone(WebhookDelivery.objects.get().payload["due_date"])


class RestCreateTests(_Base):
    def test_a_one_request_create_queues_one_task_created(self):
        response = self.client.post(TASK_URL, self.body(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.events(), [EVENT_TASK_CREATED])
        payload = WebhookDelivery.objects.get().payload
        self.assertEqual(payload["display_id"], response.json()["task"]["displayId"])

    def test_the_create_form_queues_one_task_created_when_it_is_finished(self):
        response = self.client.post(TASK_URL, self.body(is_init_task=True), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.events(), [])  # the empty draft is not a task yet
        task_id = response.json()["task"]["task_id"]
        response = self.client.put(
            TASK_URL,
            {"task_id": task_id, "title": "Filled in", "is_init_task": False, "due_date": None},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.events(), [EVENT_TASK_CREATED])
        self.assertEqual(WebhookDelivery.objects.get().payload["title"], "Filled in")


class OutsideARequestTests(_Base):
    def test_an_edit_in_autocommit_queues_a_delivery(self):
        task = self.task()
        task.title = "Renamed"
        task.save()
        self.assertEqual(self.events(), [EVENT_TASK_UPDATED])

    def test_a_create_in_a_batch_queues_one_task_created(self):
        with webhook_enqueue.batch():
            task = TaskMaster.objects.create(
                team=self.team, project=self.project, title="New", status="Open"
            )
            self.assertEqual(self.events(), [])
        self.assertEqual(self.events(), [EVENT_TASK_CREATED])
        self.assertEqual(WebhookDelivery.objects.get().payload["id"], task.task_id)

    def test_a_rolled_back_save_is_not_announced(self):
        task = self.task()
        with webhook_enqueue.batch():
            with self.assertRaises(RuntimeError), transaction.atomic():
                task.title = "Never happened"
                task.save()
                raise RuntimeError
        self.assertEqual(self.events(), [])

    def test_several_commits_in_a_batch_queue_one_delivery_with_the_last_values(self):
        task = self.task()
        with webhook_enqueue.batch():
            for title in ("One", "Two"):
                with transaction.atomic():
                    task.title = title
                    task.save()
        self.assertEqual(self.events(), [EVENT_TASK_UPDATED])
        self.assertEqual(WebhookDelivery.objects.get().payload["title"], "Two")
