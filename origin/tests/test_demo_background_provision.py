"""The demo workspace is built after sign-in returns (MIP-1879)."""

from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from origin.models.chat.unified_models import Message
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.services import demo_seeder

from .test_base import BaseAPITestCase

SPAWN = "origin.services.demo_seeder._spawn"
REINDEX = "origin.services.demo_seeder.kick_off_demo_reindex"


@override_settings(DEMO_PROVISION_IN_BACKGROUND=True)
class BackgroundProvisionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.client = APIClient()
        demo_seeder._RUNNING.clear()

    def _sign_in(self, spawned):
        with mock.patch(SPAWN, side_effect=spawned.append), mock.patch(REINDEX) as reindex:
            res = self.client.post("/api/v2/user/demo/", {"lang": "en"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.reindex = reindex
        data = res.json()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {data['access']}")
        return data

    def test_sign_in_returns_before_the_content_exists(self):
        spawned = []
        data = self._sign_in(spawned)
        self.assertEqual(data["workspace_status"], "pending")
        self.assertEqual(len(spawned), 1)
        team = TeamMaster.objects.get(team_id=data["team_id"])
        self.assertEqual(ProjectMaster.objects.filter(team=team).count(), 0)
        self.assertEqual(TeamMembers.objects.filter(team=team).count(), 5)  # visitor + 4 bots

    def test_the_build_fills_the_workspace_and_the_poll_says_ready(self):
        spawned = []
        data = self._sign_in(spawned)
        with mock.patch(REINDEX) as reindex:
            res = self.client.post("/api/v2/user/demo/workspace/")
            self.assertEqual(res.json(), {"status": "pending"})  # a thread is running: no second
            spawned[0]()
            reindex.assert_called_once()
        team = TeamMaster.objects.get(team_id=data["team_id"])
        self.assertGreater(ProjectMaster.objects.filter(team=team).count(), 0)
        self.assertGreater(TaskMaster.objects.filter(team=team).count(), 0)
        res = self.client.post("/api/v2/user/demo/workspace/")
        self.assertEqual(res.json(), {"status": "ready"})

    def test_a_second_build_adds_nothing(self):
        spawned = []
        data = self._sign_in(spawned)
        team_id = data["team_id"]
        with mock.patch(REINDEX):
            self.assertTrue(demo_seeder.provision_demo_workspace(team_id))
            counts = (
                ProjectMaster.objects.filter(team_id=team_id).count(),
                TaskMaster.objects.filter(team_id=team_id).count(),
                Message.objects.filter(channel__team_id=team_id).count(),
            )
            self.assertTrue(demo_seeder.provision_demo_workspace(team_id))
        self.assertEqual(
            counts,
            (
                ProjectMaster.objects.filter(team_id=team_id).count(),
                TaskMaster.objects.filter(team_id=team_id).count(),
                Message.objects.filter(channel__team_id=team_id).count(),
            ),
        )

    def test_a_failed_build_leaves_nothing_and_is_retried_until_three_failures(self):
        spawned = []
        data = self._sign_in(spawned)
        team_id = data["team_id"]
        with mock.patch.object(demo_seeder, "_seed_content", side_effect=RuntimeError("boom")):
            spawned[0]()  # the sign-in's own run fails and counts itself
            for _ in range(2):
                spawned.clear()
                with mock.patch(SPAWN, side_effect=spawned.append):
                    res = self.client.post("/api/v2/user/demo/workspace/")
                self.assertEqual(res.json(), {"status": "pending"})
                spawned[0]()
        self.assertEqual(ProjectMaster.objects.filter(team_id=team_id).count(), 0)
        res = self.client.post("/api/v2/user/demo/workspace/")
        self.assertEqual(res.json(), {"status": "failed"})

    def test_the_status_endpoint_is_404_for_a_real_user_and_needs_a_token(self):
        self.assertEqual(self.client.post("/api/v2/user/demo/workspace/").status_code, 401)
        self.authenticate(self.user)
        self.assertEqual(self.client.post("/api/v2/user/demo/workspace/").status_code, 404)

    def test_a_visitors_own_project_does_not_make_it_look_built(self):
        spawned = []
        data = self._sign_in(spawned)
        team = TeamMaster.objects.get(team_id=data["team_id"])
        ProjectMaster.objects.create(
            team=team,
            project_name="Mine",
            code="MNE",
            owner=team.owner,
            project_system_user=team.owner,
        )
        self.assertFalse(demo_seeder.is_demo_workspace_provisioned(team))


class InlineModeTests(BaseAPITestCase):
    def test_in_the_test_settings_the_sign_in_still_returns_a_built_workspace(self):
        with mock.patch(REINDEX):
            res = APIClient().post("/api/v2/user/demo/", {"lang": "en"}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["workspace_status"], "ready")
        self.assertGreater(ProjectMaster.objects.filter(team_id=res.json()["team_id"]).count(), 0)
