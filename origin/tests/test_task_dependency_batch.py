"""Tests for the batched dependency listing
(`GET /api/v2/task/dependency/list-for-tasks/?task_ids=1,2,3`).

The task-graph diagram fetches edges for every node in the visible
tree; the batch endpoint resolves the whole set in two indexed
queries instead of one request per node. These tests pin the
batch-specific behavior (grouping, caps, per-id keys, query count) —
the per-task ref shape itself is shared with the single view through
`_hydrate_dependency_ref`.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskDependency, TaskMaster

User = get_user_model()


class TestTaskDependencyBatchListView(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="dep-batch", email="dep-batch@test.com", password="testpass123"
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

        self.team = TeamMaster.objects.create(
            team_name="Dep Team", team_email="dep@team.com", owner=self.user
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Dep Project",
            owner=self.user,
            project_system_user=self.user,
            code="DEP",
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

        def task(title, number):
            return TaskMaster.objects.create(
                team=self.team,
                project=self.project,
                assignee=self.user,
                reporter=self.user,
                title=title,
                status="Open",
                project_task_number=number,
            )

        # a blocks b, b blocks c — so b appears on both sides.
        self.task_a = task("A", 1)
        self.task_b = task("B", 2)
        self.task_c = task("C", 3)
        self.dep_ab = TaskDependency.objects.create(
            blocker_task=self.task_a, blocked_task=self.task_b, team=self.team
        )
        self.dep_bc = TaskDependency.objects.create(
            blocker_task=self.task_b, blocked_task=self.task_c, team=self.team
        )

    def _get(self, ids):
        return self.client.get(f"/api/v2/task/dependency/list-for-tasks/?task_ids={ids}")

    def test_groups_edges_per_requested_task(self):
        ids = f"{self.task_a.task_id},{self.task_b.task_id},{self.task_c.task_id}"
        resp = self._get(ids)
        self.assertEqual(resp.status_code, 200)
        by_task = resp.json()["dependencies_by_task"]

        a = by_task[str(self.task_a.task_id)]
        self.assertEqual([d["otherTaskId"] for d in a["blocking"]], [self.task_b.task_id])
        self.assertEqual(a["blockedBy"], [])

        b = by_task[str(self.task_b.task_id)]
        self.assertEqual([d["otherTaskId"] for d in b["blocking"]], [self.task_c.task_id])
        self.assertEqual([d["otherTaskId"] for d in b["blockedBy"]], [self.task_a.task_id])

        c = by_task[str(self.task_c.task_id)]
        self.assertEqual(c["blocking"], [])
        self.assertEqual([d["otherTaskId"] for d in c["blockedBy"]], [self.task_b.task_id])

    def test_ref_shape_matches_single_view(self):
        single = self.client.get(
            f"/api/v2/task/dependency/list/?task_id={self.task_a.task_id}"
        ).json()
        batch = self._get(str(self.task_a.task_id)).json()["dependencies_by_task"][
            str(self.task_a.task_id)
        ]
        self.assertEqual(single, batch)

    def test_unknown_ids_map_to_empty_lists(self):
        resp = self._get("999999")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["dependencies_by_task"]["999999"],
            {"blocking": [], "blockedBy": []},
        )

    def test_deleted_other_endpoint_is_filtered(self):
        self.task_c.is_deleted = True
        self.task_c.save(update_fields=["is_deleted"])
        resp = self._get(str(self.task_b.task_id))
        b = resp.json()["dependencies_by_task"][str(self.task_b.task_id)]
        self.assertEqual(b["blocking"], [])  # c is tombstoned
        self.assertEqual(len(b["blockedBy"]), 1)  # a is alive

    def test_400_on_non_integer_ids(self):
        self.assertEqual(self._get("1,abc").status_code, 400)

    def test_400_when_over_the_cap(self):
        ids = ",".join(str(i) for i in range(1, 503))
        self.assertEqual(self._get(ids).status_code, 400)

    def test_empty_ids_returns_empty_map(self):
        resp = self._get("")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["dependencies_by_task"], {})

    def test_401_when_unauthenticated(self):
        self.client.credentials()
        resp = self._get(str(self.task_a.task_id))
        self.assertIn(resp.status_code, (401, 403))

    def test_query_count_is_flat_regardless_of_task_count(self):
        # 1 auth-user fetch + 1 blocking query + 1 blockedBy query —
        # the whole point of the batch. Would be 2N+1 via the single
        # view.
        ids = f"{self.task_a.task_id},{self.task_b.task_id},{self.task_c.task_id}"
        with self.assertNumQueries(3):
            resp = self._get(ids)
        self.assertEqual(resp.status_code, 200)
