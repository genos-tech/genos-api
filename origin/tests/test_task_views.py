from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers

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
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

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

    def test_create_task_response_includes_display_id(self):
        # POST must surface the computed `display_id` (like PUT and the
        # project-tasks list endpoint) so a single-POST create can render
        # the friendly "<code>-<n>" id without waiting for a refetch.
        from origin.models.task.task_models import TaskMaster

        response = self._create_task()
        self.assertEqual(response.status_code, 201)
        self.assertIn("displayId", response.data["task"])
        task = TaskMaster.objects.get(task_id=response.data["task"]["task_id"])
        self.assertEqual(response.data["task"]["displayId"], task.display_id)
        # The serialized display id is non-empty (either "<code>-<n>" once
        # the post-save signal assigns a number, or the "#<id>" fallback).
        self.assertTrue(response.data["task"]["displayId"])

    def test_create_task_missing_required_field(self):
        # A missing required field is a client error: a clean 400, not a
        # 500 from an unguarded request.data[...] KeyError.
        payload = self._task_payload()
        del payload["title"]
        response = self.client.post("/api/v2/task/", payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("title", str(response.data).lower())

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

    def _search_team_tasks(self, statuses="open,wip,blocked,pending", include_all="false"):
        return self.client.get(
            "/api/v2/search/teamTasks/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "statuses": statuses,
                "top_n": -1,
                "include_all": include_all,
            },
        )

    def test_search_team_tasks_includes_blocked(self):
        blocked = self._create_task(title="Blocked task", status="Blocked").data["task"]
        response = self._search_team_tasks()
        self.assertEqual(response.status_code, 200)
        self.assertIn(blocked["task_id"], {t["taskId"] for t in response.data})

    def test_search_team_tasks_carries_is_milestone(self):
        from origin.models.task.task_models import TaskMaster

        plain = self._create_task(title="Plain task").data["task"]
        backing = self._create_task(title="Milestone backing task").data["task"]
        TaskMaster.objects.filter(task_id=backing["task_id"]).update(is_milestone=True)

        response = self._search_team_tasks()
        by_id = {t["taskId"]: t for t in response.data}
        self.assertFalse(by_id[plain["task_id"]]["isMilestone"])
        self.assertTrue(by_id[backing["task_id"]]["isMilestone"])

    def test_search_team_tasks_hides_children_of_closed_parent(self):
        parent = self._create_task(title="Closed parent", status="Closed").data["task"]
        child = self._create_task(title="Open child", parent_task_id=parent["task_id"]).data["task"]
        grandchild = self._create_task(
            title="Open grandchild", parent_task_id=child["task_id"]
        ).data["task"]
        top = self._create_task(title="Open top-level").data["task"]

        response = self._search_team_tasks()
        ids = {t["taskId"] for t in response.data}
        self.assertIn(top["task_id"], ids)
        self.assertNotIn(child["task_id"], ids)
        self.assertNotIn(grandchild["task_id"], ids)

    def test_search_team_tasks_hides_subtask_of_closed_intermediate_parent(self):
        # Root open, middle closed, leaf open: the old root-only check
        # let the leaf through; the parent-chain walk must hide it.
        root = self._create_task(title="Open root").data["task"]
        middle = self._create_task(
            title="Closed middle", status="Closed", parent_task_id=root["task_id"]
        ).data["task"]
        leaf = self._create_task(title="Open leaf", parent_task_id=middle["task_id"]).data["task"]

        response = self._search_team_tasks()
        ids = {t["taskId"] for t in response.data}
        self.assertIn(root["task_id"], ids)
        self.assertNotIn(leaf["task_id"], ids)

    def test_search_team_tasks_include_all_keeps_closed_parent_children(self):
        parent = self._create_task(title="Closed parent", status="Closed").data["task"]
        child = self._create_task(title="Open child", parent_task_id=parent["task_id"]).data["task"]

        response = self._search_team_tasks(include_all="true")
        self.assertIn(child["task_id"], {t["taskId"] for t in response.data})

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
