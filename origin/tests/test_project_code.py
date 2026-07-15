"""Unit tests for the project-code derivation algorithm + integration
tests proving the new project-scoped task display IDs flow end-to-end:
project creation auto-assigns a code, task creation auto-assigns a
per-project number, and `display_id` lands in API responses."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services.project_code import derive_project_code

User = get_user_model()


class TestDeriveProjectCode(TestCase):
    """Pure algorithm tests — no DB involvement."""

    def test_multi_word_uses_initials(self):
        self.assertEqual(derive_project_code("Genos Core", set()), "GC")
        self.assertEqual(derive_project_code("Marketing Site", set()), "MS")
        self.assertEqual(derive_project_code("API Backend Team", set()), "ABT")

    def test_single_word_uses_first_three_letters(self):
        self.assertEqual(derive_project_code("Marketing", set()), "MAR")
        self.assertEqual(derive_project_code("Genos", set()), "GEN")
        self.assertEqual(derive_project_code("Engineering", set()), "ENG")

    def test_camel_case_splits_at_boundaries(self):
        self.assertEqual(derive_project_code("GenosCore", set()), "GC")
        self.assertEqual(derive_project_code("WebApp", set()), "WA")

    def test_non_alpha_separators_split(self):
        self.assertEqual(derive_project_code("marketing-site", set()), "MS")
        self.assertEqual(derive_project_code("api_backend", set()), "AB")
        self.assertEqual(derive_project_code("Web/App", set()), "WA")

    def test_short_word_pads_minimum_length(self):
        # "Web" → "WEB" (3 letters); "It" → "IT" (2 letters, meets MIN_LEN)
        self.assertEqual(derive_project_code("It", set()), "IT")
        self.assertEqual(derive_project_code("AI", set()), "AI")

    def test_empty_or_symbolic_input_falls_back(self):
        self.assertEqual(derive_project_code("", set()), "PRJ")
        self.assertEqual(derive_project_code("---", set()), "PRJ")
        self.assertEqual(derive_project_code("!!!", set()), "PRJ")

    def test_collision_appends_numeric_suffix(self):
        self.assertEqual(derive_project_code("Genos", {"GEN"}), "GEN2")
        self.assertEqual(derive_project_code("Genos", {"GEN", "GEN2"}), "GEN3")
        self.assertEqual(derive_project_code("Genos", {"GEN", "GEN2", "GEN3"}), "GEN4")

    def test_collision_truncates_base_to_fit_max_length(self):
        # Base "ABCDEF" (6 chars) collides → suffix "2" requires
        # truncating base to "ABCDE" + "2" = "ABCDE2" (still 6).
        result = derive_project_code("AlphaBravoCharlieDeltaEchoFoxtrot", {"ABC"})
        # Multi-word so initials → "ABC" cap-3, taken → "ABC2"
        self.assertEqual(result, "ABC2")

    def test_collision_with_taken_uses_existing(self):
        # Doesn't modify the passed set, doesn't pick a taken code.
        taken = {"GC", "GC2"}
        result = derive_project_code("Genos Core", taken)
        self.assertEqual(result, "GC3")
        self.assertEqual(taken, {"GC", "GC2"})  # unchanged


class TestProjectAndTaskAutoAssignment(TestCase):
    """Integration: project create → code; task create → number."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="alice",
            email="alice@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.user).access_token}"
        )
        self.team = TeamMaster.objects.create(
            team_name="Alice's Team",
            team_email="alice@team.com",
            owner=self.user,
        )

    def _create_project(self, name: str) -> int:
        resp = self.client.post(
            "/api/v2/project/",
            {
                "team": str(self.team.team_id),
                "project_name": name,
                "owner": self.user.id,
                "project_system_user": self.user.id,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        return resp.data["project_id"]

    def _create_task(self, project_id: int, title: str = "T") -> dict:
        resp = self.client.post(
            "/api/v2/task/",
            {
                "team": str(self.team.team_id),
                "project": project_id,
                "assignee": self.user.id,
                "reporter": self.user.id,
                "title": title,
                "priority": "Medium",
                "effort_level": "Medium",
                "status": "Open",
                "content": {},
                "due_date": "2026-12-31",
                "links": [],
                "tags": [],
                "is_init_task": False,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        return resp.data["task"]

    def test_project_create_auto_assigns_code(self):
        pid = self._create_project("Engineering")
        proj = ProjectMaster.objects.get(project_id=pid)
        # ProjectMembers needed for task creation
        ProjectMembers.objects.create(team=self.team, project=proj, attendee=self.user)
        self.assertEqual(proj.code, "ENG")

    def test_collision_within_team_appends_suffix(self):
        pid1 = self._create_project("Engineering")
        pid2 = self._create_project("Engineering Backend")
        # "ENG" taken → "EB" (multi-word initials) → no collision
        self.assertEqual(ProjectMaster.objects.get(project_id=pid1).code, "ENG")
        self.assertEqual(ProjectMaster.objects.get(project_id=pid2).code, "EB")

    def test_same_base_within_team_appends_numeric_suffix(self):
        pid1 = self._create_project("Engineering")
        pid2 = self._create_project("Energy")  # → "ENE" — different base; not a collision
        pid3 = self._create_project("Engine")  # → "ENG" — collides with #1
        self.assertEqual(ProjectMaster.objects.get(project_id=pid1).code, "ENG")
        self.assertEqual(ProjectMaster.objects.get(project_id=pid2).code, "ENE")
        self.assertEqual(ProjectMaster.objects.get(project_id=pid3).code, "ENG2")

    def test_task_create_auto_assigns_sequential_number(self):
        pid = self._create_project("Engineering")
        proj = ProjectMaster.objects.get(project_id=pid)
        ProjectMembers.objects.create(team=self.team, project=proj, attendee=self.user)

        t1 = self._create_task(pid)
        t2 = self._create_task(pid)
        t3 = self._create_task(pid)

        for task_dict, expected_num in [(t1, 1), (t2, 2), (t3, 3)]:
            task = TaskMaster.objects.get(task_id=task_dict["task_id"])
            self.assertEqual(task.project_task_number, expected_num)
            self.assertEqual(task.display_id, f"ENG-{expected_num}")

    def test_task_numbers_independent_per_project(self):
        # Two projects' task numbers don't share a sequence.
        p1 = self._create_project("Alpha")
        p2 = self._create_project("Beta")
        proj1 = ProjectMaster.objects.get(project_id=p1)
        proj2 = ProjectMaster.objects.get(project_id=p2)
        ProjectMembers.objects.create(team=self.team, project=proj1, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=proj2, attendee=self.user)

        self._create_task(p1)
        self._create_task(p1)
        first_b = self._create_task(p2)

        # Project Beta's first task is number 1, not 3.
        task = TaskMaster.objects.get(task_id=first_b["task_id"])
        self.assertEqual(task.project_task_number, 1)
        self.assertEqual(task.display_id, "BET-1")

    def test_display_id_in_get_team_tasks_response(self):
        pid = self._create_project("Engineering")
        proj = ProjectMaster.objects.get(project_id=pid)
        ProjectMembers.objects.create(team=self.team, project=proj, attendee=self.user)
        self._create_task(pid, title="First")
        self._create_task(pid, title="Second")

        resp = self.client.get("/api/v2/task/getTeamTasks/", {"team_id": str(self.team.team_id)})
        self.assertEqual(resp.status_code, 200)
        display_ids = sorted(row["displayId"] for row in resp.data)
        self.assertEqual(display_ids, ["ENG-1", "ENG-2"])

    def test_display_id_falls_back_to_hash_when_no_project_code(self):
        # Orphan task (no project): display_id falls back to "#<task_id>".
        # We bypass the API to construct this state directly.
        task = TaskMaster.objects.create(
            team=self.team,
            project=None,  # No project
            assignee=self.user,
            reporter=self.user,
            title="orphan",
            status="Open",
        )
        self.assertEqual(task.display_id, f"#{task.task_id}")


class TestTaskProjectMove(TestCase):
    """A task PUT that changes `project` must re-claim a number in the
    destination. Numbers are unique per project, so carrying the source's
    number over collides with whatever task already holds it there."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="alice",
            email="alice@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.user).access_token}"
        )
        self.team = TeamMaster.objects.create(
            team_name="Alice's Team",
            team_email="alice@team.com",
            owner=self.user,
        )
        self.project_a = self._create_project("Alpha")
        self.project_b = self._create_project("Beta")

    def _create_project(self, name: str) -> int:
        resp = self.client.post(
            "/api/v2/project/",
            {
                "team": str(self.team.team_id),
                "project_name": name,
                "owner": self.user.id,
                "project_system_user": self.user.id,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        project_id = resp.data["project_id"]
        ProjectMembers.objects.create(
            team=self.team,
            project=ProjectMaster.objects.get(project_id=project_id),
            attendee=self.user,
        )
        return project_id

    def _create_task(self, project_id: int, title: str = "T", is_init_task: bool = False) -> dict:
        resp = self.client.post(
            "/api/v2/task/",
            {
                "team": str(self.team.team_id),
                "project": project_id,
                "assignee": self.user.id,
                "reporter": self.user.id,
                "title": title,
                "priority": "Medium",
                "effort_level": "Medium",
                "status": "Open",
                "content": {},
                "due_date": "2026-12-31",
                "links": [],
                "tags": [],
                "is_init_task": is_init_task,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        return resp.data["task"]

    def _put_task(self, task_id: int, project_id: int, title: str = "T"):
        return self.client.put(
            "/api/v2/task/",
            {
                "team": str(self.team.team_id),
                "task_id": task_id,
                "project": project_id,
                "assignee": self.user.id,
                "reporter": self.user.id,
                "title": title,
                "priority": "Medium",
                "effort_level": "Medium",
                "status": "Open",
                "content": {},
                "due_date": "2026-12-31",
                "links": [],
                "tags": [],
                "is_init_task": False,
            },
            format="json",
        )

    def test_move_to_project_whose_number_is_taken(self):
        """The reported bug: the create form bootstraps an empty task in the
        current project, the user switches the picker to another project, and
        the finalizing PUT carried the source project's number into a slot
        the destination had already used → 400 "must make a unique set"."""
        # Beta already owns number 1, so the naive carry-over collides.
        self._create_task(self.project_b, title="Beta's first")
        moving = self._create_task(self.project_a, title="draft", is_init_task=True)
        self.assertEqual(
            TaskMaster.objects.get(task_id=moving["task_id"]).project_task_number, 1
        )

        resp = self._put_task(moving["task_id"], self.project_b, title="real title")

        self.assertEqual(resp.status_code, 200, resp.data)
        task = TaskMaster.objects.get(task_id=moving["task_id"])
        self.assertEqual(task.project_id, self.project_b)
        # Renumbered into Beta's sequence, not left holding Alpha's 1.
        self.assertEqual(task.project_task_number, 2)
        self.assertEqual(task.display_id, "BET-2")
        # The response carries the post-move display_id — the frontend
        # stamps it onto the new row and outgoing socket payloads.
        self.assertEqual(resp.data["task"]["displayId"], "BET-2")

    def test_move_to_empty_project_starts_that_projects_sequence(self):
        self._create_task(self.project_a, title="Alpha's first")
        moving = self._create_task(self.project_a, title="draft", is_init_task=True)
        self.assertEqual(
            TaskMaster.objects.get(task_id=moving["task_id"]).project_task_number, 2
        )

        resp = self._put_task(moving["task_id"], self.project_b)

        self.assertEqual(resp.status_code, 200, resp.data)
        task = TaskMaster.objects.get(task_id=moving["task_id"])
        # Beta's first task → 1, NOT Alpha's 2 carried over.
        self.assertEqual(task.project_task_number, 1)
        self.assertEqual(task.display_id, "BET-1")

    def test_update_within_same_project_keeps_number(self):
        """The common path — every ordinary edit PUTs `project` unchanged.
        A false-positive move detection here would renumber (and change the
        display_id of) every task on every save."""
        task_data = self._create_task(self.project_a, title="first")
        self._create_task(self.project_a, title="second")

        resp = self._put_task(task_data["task_id"], self.project_a, title="edited")

        self.assertEqual(resp.status_code, 200, resp.data)
        task = TaskMaster.objects.get(task_id=task_data["task_id"])
        self.assertEqual(task.title, "edited")
        self.assertEqual(task.project_task_number, 1)
        self.assertEqual(task.display_id, "ALP-1")

    def test_move_frees_the_source_number_for_reuse(self):
        moving = self._create_task(self.project_a, title="draft", is_init_task=True)
        self._put_task(moving["task_id"], self.project_b)

        # Alpha's 1 is vacated by the move; Alpha's next create takes it
        # rather than 500ing on the unique constraint.
        next_a = self._create_task(self.project_a, title="after the move")
        self.assertEqual(
            TaskMaster.objects.get(task_id=next_a["task_id"]).project_task_number, 1
        )
