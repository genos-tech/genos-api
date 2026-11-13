"""A task made through the public REST API reaches the project chat (MIP-1462).

The key has no browser to send the "task created" card the app would, so
`POST /api/public/v1/tasks/` posts it from the server (see `task_card_post`).
"""

from unittest import mock

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.common.api_key_models import SCOPE_WRITE, ApiKey, generate_key, hash_key
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster

from .test_base import BaseAPITestCase

URL = "/api/public/v1/tasks/"


class PublicApiTaskCardTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Public",
            owner=self.user,
            code="PUB",
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.channel = Channel.objects.get(kind=ChannelKind.PM, legacy_chat_id=self.project.pk)
        raw = generate_key()
        ApiKey.objects.create(
            user=self.user,
            name="card",
            prefix=raw[:11],
            key_hash=hash_key(raw),
            scope=SCOPE_WRITE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")

    def _post(self, **body):
        return self.client.post(
            URL,
            {
                "title": "From an integration",
                "project_id": self.project.project_id,
                # A personal token spans every team the user is in, so it has to name one.
                "team_id": str(self.team.team_id),
                **body,
            },
            format="json",
        )

    def _cards(self):
        return list(Message.objects.filter(channel=self.channel, is_thread_reply=False))

    def test_a_created_task_is_listed_in_the_project_chat(self):
        res = self._post(priority="High")

        self.assertEqual(res.status_code, 201, res.data)
        task = TaskMaster.objects.get(task_id=res.data["id"])
        (card,) = self._cards()
        self.assertEqual(card.task_id, task.task_id)
        self.assertEqual(card.sender_id, self.user.id)  # the key's owner
        self.assertEqual(card.body_text, "From an integration")
        text = "".join(c.get("text", "") or "" for b in card.body for c in b.get("content", []))
        self.assertIn("🧾 From an integration", text)
        self.assertIn("Priority: High", text)

    def test_a_task_with_no_priority_still_posts(self):
        """The common integration call sends a title and a project and nothing else."""
        res = self._post()

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(len(self._cards()), 1)

    def test_a_failing_post_never_fails_the_create(self):
        with mock.patch(
            "origin.views.chat.message_views._allocate_seq_and_create_message_with_activities",
            side_effect=RuntimeError("chat is down"),
        ):
            res = self._post()

        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(TaskMaster.objects.filter(task_id=res.data["id"]).exists())
        self.assertEqual(self._cards(), [])

    def test_a_refused_create_posts_nothing(self):
        res = self.client.post(URL, {"title": "", "team_id": str(self.team.team_id)}, format="json")

        self.assertEqual(res.status_code, 400)
        self.assertEqual(self._cards(), [])
