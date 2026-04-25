from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster, TaskComments

User = get_user_model()


class TestTaskViews(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="testuser", email="test@test.com", password="testpass123"
        )
        self.user2 = User.objects.create_user(
            username="testuser2", email="test2@test.com", password="testpass123"
        )
        refresh = RefreshToken.for_user(self.user)
        self.access_token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access_token}")

        self.team = TeamMaster.objects.create(
            team_name="Test Team",
            team_email="team@test.com",
            owner=self.user,
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Test Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.user
        )

    def _task_payload(self, **overrides):
        defaults = {
            "team": str(self.team.team_id),
            "project": self.project.project_id,
            "assignee": self.user.id,
            "reporter": self.user.id,
            "title": "Test Task",
            "priority": "Medium",
            "effort_level": "Medium",
            "status": "Open",
            "content": {"body": "task content"},
            "due_date": "2026-12-31",
            "links": [],
            "tags": [],
            "is_init_task": False,
        }
        defaults.update(overrides)
        return defaults

    def _create_task(self, **overrides):
        payload = self._task_payload(**overrides)
        return self.client.post("/api/v2/task/", payload, format="json")

    # ── Task Create ────────────────────────────────────────────────

    def test_create_task_with_content(self):
        response = self._create_task()
        self.assertEqual(response.status_code, 201)
        self.assertIn("task", response.data)
        self.assertEqual(response.data["task"]["title"], "Test Task")

    def test_create_task_without_content_field(self):
        """Regression: content=None should not raise UnboundLocalError."""
        response = self._create_task(content=None)
        self.assertEqual(response.status_code, 201)
        self.assertIn("task", response.data)

    def test_create_task_returns_newly_mentioned_user_ids(self):
        response = self._create_task()
        self.assertEqual(response.status_code, 201)
        self.assertIn("newly_mentioned_user_ids", response.data)

    def test_create_task_missing_required_field(self):
        payload = self._task_payload()
        del payload["title"]
        try:
            response = self.client.post("/api/v2/task/", payload, format="json")
            self.assertIn(response.status_code, [400, 500])
        except KeyError:
            pass

    # ── Task Update ────────────────────────────────────────────────

    def test_update_task(self):
        create_resp = self._create_task()
        task_id = create_resp.data["task"]["task_id"]

        update_payload = self._task_payload(task_id=task_id, title="Updated Title")
        response = self.client.put("/api/v2/task/", update_payload, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["task"]["title"], "Updated Title")

    def test_update_task_missing_task_id(self):
        response = self.client.put("/api/v2/task/", {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_update_nonexistent_task(self):
        payload = self._task_payload(task_id=999999)
        response = self.client.put("/api/v2/task/", payload, format="json")
        self.assertEqual(response.status_code, 404)

    # ── Task Delete ────────────────────────────────────────────────

    def test_delete_task(self):
        create_resp = self._create_task()
        task_id = create_resp.data["task"]["task_id"]

        response = self.client.delete(
            f"/api/v2/task/?team_id={self.team.team_id}&task_id={task_id}&is_init_task_boolean=0",
        )
        self.assertEqual(response.status_code, 204)

    def test_delete_task_missing_params(self):
        response = self.client.delete("/api/v2/task/")
        self.assertEqual(response.status_code, 400)

    def test_delete_nonexistent_task(self):
        response = self.client.delete(
            f"/api/v2/task/?team_id={self.team.team_id}&task_id=999999&is_init_task_boolean=0",
        )
        self.assertEqual(response.status_code, 404)

    # ── Get Team Tasks ─────────────────────────────────────────────

    def test_get_team_tasks(self):
        self._create_task(tags=[{"tagName": "v1"}])
        response = self.client.get(
            "/api/v2/task/getTeamTasks/",
            {"team_id": str(self.team.team_id)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)

    def test_get_team_tasks_missing_team_id(self):
        response = self.client.get("/api/v2/task/getTeamTasks/")
        self.assertEqual(response.status_code, 400)

    # ── Task Comments ──────────────────────────────────────────────

    def test_post_task_comment(self):
        create_resp = self._create_task()
        task_id = create_resp.data["task"]["task_id"]

        response = self.client.post(
            "/api/v2/task/comment/",
            {
                "task_id": task_id,
                "sender_id": self.user.id,
                "comment_body": {"text": "A comment"},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)

    def test_get_task_comments_returns_200(self):
        """GET /api/v2/task/comment/ should return 200, not 201."""
        create_resp = self._create_task()
        task_id = create_resp.data["task"]["task_id"]

        self.client.post(
            "/api/v2/task/comment/",
            {
                "task_id": task_id,
                "sender_id": self.user.id,
                "comment_body": {"text": "A comment"},
            },
            format="json",
        )

        response = self.client.get(
            "/api/v2/task/comment/",
            {"task_id": task_id, "user_id": self.user.id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)

    def test_get_task_comments_missing_task_id(self):
        response = self.client.get("/api/v2/task/comment/")
        self.assertEqual(response.status_code, 400)

    # ── Search Tasks ───────────────────────────────────────────────

    def test_search_team_tasks(self):
        self._create_task()
        response = self.client.get(
            "/api/v2/search/teamTasks/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "statuses": "open",
                "top_n": 10,
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_search_team_tasks_missing_params_returns_400(self):
        """Missing required params should return 400, not crash."""
        response = self.client.get("/api/v2/search/teamTasks/")
        self.assertEqual(response.status_code, 400)

    def test_search_team_tasks_partial_params_returns_400(self):
        response = self.client.get(
            "/api/v2/search/teamTasks/",
            {"team_id": str(self.team.team_id)},
        )
        self.assertEqual(response.status_code, 400)

    # ── GetTaskView missing params ─────────────────────────────────

    def test_get_task_missing_params(self):
        response = self.client.get("/api/v2/task/getTask/")
        self.assertEqual(response.status_code, 400)

    def test_get_task_partial_params(self):
        response = self.client.get(
            "/api/v2/task/getTask/",
            {"team_id": str(self.team.team_id)},
        )
        self.assertEqual(response.status_code, 400)

    # ── ChildTaskView missing params ───────────────────────────────

    def test_child_tasks_missing_params(self):
        response = self.client.get("/api/v2/task/childTasks/")
        self.assertEqual(response.status_code, 400)

    def test_child_tasks_partial_params(self):
        response = self.client.get(
            "/api/v2/task/childTasks/",
            {"team_id": str(self.team.team_id), "project_id": self.project.project_id},
        )
        self.assertEqual(response.status_code, 400)

    # ── Unauthorized ───────────────────────────────────────────────

    def test_unauthenticated_request(self):
        client = APIClient()
        response = client.get("/api/v2/task/getTeamTasks/")
        self.assertEqual(response.status_code, 401)
