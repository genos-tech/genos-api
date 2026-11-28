"""The REST and v1 writers accept only the six status values (MIP-2391).

A wrong value is a 400 that lists the valid ones; a save that leaves a row's own stored status
as it is (even an odd one) is never refused, and nothing is migrated.
"""

from origin.models.common.api_key_models import SCOPE_WRITE, ApiKey, generate_key, hash_key
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services.task_status_values import VALID_STATUS_VALUES, status_value_error
from origin.tests.test_base import BaseAPITestCase

LISTED = "Open, WIP, Blocked, Pending, Closed, Rejected, Deleted"


class StatusValueHelperTests(BaseAPITestCase):
    def test_the_system_values_pass_and_others_fail(self):
        for value in VALID_STATUS_VALUES:
            self.assertIsNone(status_value_error(value))
        for value in ("banana", "open", "", None, 3, ["Open"]):
            self.assertIn(LISTED, status_value_error(value))

    def test_the_stored_value_passes_whatever_it_is(self):
        self.assertIsNone(status_value_error("In review", "In review"))
        self.assertIsNotNone(status_value_error("In review", "Open"))


class StatusRestWritersTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate(self.user)
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def _task_payload(self, **overrides):
        data = {
            "team": str(self.team.team_id),
            "project": self.project.project_id,
            "assignee": self.user.id,
            "reporter": self.user.id,
            "title": "T",
            "priority": "Normal",
            "effort_level": "Low",
            "status": "Open",
            "content": {"body": "x"},
            "due_date": "2026-12-31",
            "links": [],
            "tags": [],
            "is_init_task": False,
        }
        data.update(overrides)
        return data

    def _odd_task(self, status="In review"):
        return TaskMaster.objects.create(
            team=self.team, project=self.project, title="Old", status=status, reporter=self.user
        )

    # ── tasks ──
    def test_task_create_refuses_a_wrong_status(self):
        res = self.client.post("/api/v2/task/", self._task_payload(status="banana"), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn(LISTED, res.data["error"])
        self.assertFalse(TaskMaster.objects.filter(title="T").exists())

    def test_task_create_accepts_each_valid_status(self):
        for value in VALID_STATUS_VALUES:
            res = self.client.post(
                "/api/v2/task/", self._task_payload(status=value, title=value), format="json"
            )
            self.assertEqual(res.status_code, 201, value)

    def test_task_put_refuses_a_wrong_status_and_changes_nothing(self):
        task = self._odd_task("Open")
        res = self.client.put(
            "/api/v2/task/",
            self._task_payload(task_id=task.task_id, status="banana", title="Renamed"),
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn(LISTED, res.data["error"])
        task.refresh_from_db()
        self.assertEqual((task.status, task.title), ("Open", "Old"))

    def test_task_put_that_restates_an_existing_odd_status_is_not_rejected(self):
        task = self._odd_task("In review")
        res = self.client.put(
            "/api/v2/task/",
            self._task_payload(task_id=task.task_id, status="In review", title="Renamed"),
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        task.refresh_from_db()
        self.assertEqual((task.status, task.title), ("In review", "Renamed"))

    def test_task_put_moves_an_odd_row_to_a_valid_status(self):
        task = self._odd_task("In review")
        res = self.client.put(
            "/api/v2/task/",
            self._task_payload(task_id=task.task_id, status="WIP"),
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "WIP")

    # ── milestones ──
    def test_milestone_create_and_patch(self):
        res = self.client.post(
            "/api/v2/milestone/",
            {"project_id": self.project.project_id, "title": "M", "status": "banana"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn(LISTED, res.data["error"])
        self.assertFalse(MilestoneMaster.objects.filter(title="M").exists())

        res = self.client.post(
            "/api/v2/milestone/",
            {"project_id": self.project.project_id, "title": "M", "status": "WIP"},
            format="json",
        )
        self.assertEqual(res.status_code, 201)

        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="Old", reporter=self.user, status="Open"
        )
        url = f"/api/v2/milestone/{milestone.milestone_id}/"
        res = self.client.patch(url, {"status": "banana"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn(LISTED, res.data["error"])
        milestone.refresh_from_db()
        self.assertEqual(milestone.status, "Open")
        self.assertEqual(
            self.client.patch(url, {"status": "Closed"}, format="json").status_code, 200
        )

    def test_milestone_patch_restating_an_odd_status_is_not_rejected(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="Old", reporter=self.user, status="Doing"
        )
        res = self.client.patch(
            f"/api/v2/milestone/{milestone.milestone_id}/",
            {"status": "Doing", "title": "Renamed"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        milestone.refresh_from_db()
        self.assertEqual((milestone.status, milestone.title), ("Doing", "Renamed"))


class StatusV1WritersTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        raw = generate_key()
        ApiKey.objects.create(
            user=self.user, name="k", key_hash=hash_key(raw), prefix=raw[:11], scope=SCOPE_WRITE
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")

    def _create(self, **extra):
        return self.client.post(
            f"/api/public/v1/tasks/?team_id={self.team.team_id}",
            {"title": "V1", "project_id": self.project.project_id, **extra},
            format="json",
        )

    def test_create_refuses_a_wrong_status_and_defaults_to_open(self):
        res = self._create(status="banana")
        self.assertEqual(res.status_code, 400)
        self.assertIn(LISTED, res.data["error"])
        self.assertFalse(TaskMaster.objects.filter(title="V1").exists())
        res = self._create()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["status"], "Open")
        self.assertEqual(self._create(status="Pending").status_code, 201)

    def test_patch_refuses_a_wrong_status_but_not_the_stored_one(self):
        task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Old",
            status="In review",
            reporter=self.user,
        )
        url = f"/api/public/v1/tasks/{task.task_id}/?team_id={self.team.team_id}"
        res = self.client.patch(url, {"status": "banana"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn(LISTED, res.data["error"])
        res = self.client.patch(url, {"status": "In review", "title": "Renamed"}, format="json")
        self.assertEqual(res.status_code, 200)
        task.refresh_from_db()
        self.assertEqual((task.status, task.title), ("In review", "Renamed"))
