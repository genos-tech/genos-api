"""`effectiveStatus` on the dependency refs (MIP-1549).

A blocker under a Closed or Deleted ancestor reads as Closed or Deleted (the closed-ancestor rule,
`services/task_ancestry`), and the server's auto-Blocked flip already honours it (MIP-1544). The
dependency endpoints used to send only the blocker's OWN status, so the client's Blocked badge could
say Blocked while the server said Open. `effectiveStatus` carries the ancestor reading; `status`
stays the task's own.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskDependency, TaskMaster

User = get_user_model()


class TestDependencyEffectiveStatus(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="dep-eff", email="dep-eff@test.com", password="testpass123"
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
        self.team = TeamMaster.objects.create(
            team_name="Eff Team", team_email="eff@team.com", owner=self.user
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Eff Project",
            owner=self.user,
            project_system_user=self.user,
            code="EFF",
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self._n = 0

        # parent (open) > blocker (WIP) ; blocker blocks `dependent`; `plain` blocks `dependent` too.
        self.parent = self._task("Parent", "Open")
        self.blocker = self._task("Blocker", "WIP", parent=self.parent)
        self.plain = self._task("Plain blocker", "Open")
        self.dependent = self._task("Dependent", "Open")
        TaskDependency.objects.create(
            blocker_task=self.blocker, blocked_task=self.dependent, team=self.team
        )
        TaskDependency.objects.create(
            blocker_task=self.plain, blocked_task=self.dependent, team=self.team
        )

    def _task(self, title, status, parent=None):
        self._n += 1
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user,
            reporter=self.user,
            title=title,
            status=status,
            parent_task_id=parent.task_id if parent else None,
            project_task_number=self._n,
        )

    def _set_parent(self, status):
        self.parent.status = status
        self.parent.save(update_fields=["status"])

    def _single(self, task):
        resp = self.client.get(f"/api/v2/task/dependency/list/?task_id={task.task_id}")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def _batch(self, *tasks):
        ids = ",".join(str(t.task_id) for t in tasks)
        resp = self.client.get(f"/api/v2/task/dependency/list-for-tasks/?task_ids={ids}")
        self.assertEqual(resp.status_code, 200)
        return resp.json()["dependencies_by_task"]

    def _by_other(self, refs):
        return {r["otherTaskId"]: r for r in refs}

    def test_blocker_under_closed_parent_reads_closed_but_keeps_its_own_status(self):
        self._set_parent("Closed")
        refs = self._by_other(self._single(self.dependent)["blockedBy"])
        blocker = refs[self.blocker.task_id]
        self.assertEqual(blocker["effectiveStatus"], "Closed")
        self.assertEqual(blocker["status"]["status"], "WIP")  # own status is never rewritten
        self.assertEqual(refs[self.plain.task_id]["effectiveStatus"], "Open")

    def test_blocker_under_deleted_parent_reads_deleted(self):
        self._set_parent("Deleted")
        refs = self._by_other(self._single(self.dependent)["blockedBy"])
        self.assertEqual(refs[self.blocker.task_id]["effectiveStatus"], "Deleted")

    def test_no_closed_ancestor_reads_own_status(self):
        refs = self._by_other(self._single(self.dependent)["blockedBy"])
        self.assertEqual(refs[self.blocker.task_id]["effectiveStatus"], "WIP")
        self.assertEqual(refs[self.plain.task_id]["effectiveStatus"], "Open")

    def test_reopening_the_parent_flips_it_back(self):
        self._set_parent("Closed")
        self.assertEqual(
            self._by_other(self._single(self.dependent)["blockedBy"])[self.blocker.task_id][
                "effectiveStatus"
            ],
            "Closed",
        )
        self._set_parent("Open")
        self.assertEqual(
            self._by_other(self._single(self.dependent)["blockedBy"])[self.blocker.task_id][
                "effectiveStatus"
            ],
            "WIP",
        )

    def test_blocking_side_carries_it_too(self):
        # The blocker's own `blocking` list names the dependent; the dependent sits under a
        # Closed parent here, so it reads Closed.
        child_parent = self._task("Other parent", "Closed")
        self.dependent.parent_task_id = child_parent.task_id
        self.dependent.save(update_fields=["parent_task_id"])
        refs = self._by_other(self._single(self.blocker)["blocking"])
        self.assertEqual(refs[self.dependent.task_id]["effectiveStatus"], "Closed")

    def test_batch_endpoint_carries_it(self):
        self._set_parent("Closed")
        by_task = self._batch(self.dependent, self.blocker)
        refs = self._by_other(by_task[str(self.dependent.task_id)]["blockedBy"])
        self.assertEqual(refs[self.blocker.task_id]["effectiveStatus"], "Closed")
        self.assertEqual(refs[self.blocker.task_id]["status"]["status"], "WIP")
        self.assertEqual(refs[self.plain.task_id]["effectiveStatus"], "Open")

    def test_single_and_batch_agree(self):
        self._set_parent("Closed")
        single = self._single(self.dependent)
        batch = self._batch(self.dependent)[str(self.dependent.task_id)]
        # Neither endpoint orders its edges (no ORDER BY), so compare each side by otherTaskId.
        for side in ("blocking", "blockedBy"):
            single[side].sort(key=lambda r: r["otherTaskId"])
            batch[side].sort(key=lambda r: r["otherTaskId"])
        self.assertEqual(len(single["blockedBy"]), 2)  # not vacuous: both blockers are listed
        self.assertEqual(single, batch)

    def test_created_edge_echo_carries_it(self):
        self._set_parent("Closed")
        extra = self._task("Extra", "Open", parent=self.parent)
        resp = self.client.post(
            "/api/v2/task/dependency/",
            {"blocker_task_id": self.dependent.task_id, "blocked_task_id": extra.task_id},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["effectiveStatus"], "Closed")
        self.assertEqual(resp.json()["status"]["status"], "Open")

    def test_query_count_does_not_grow_with_the_edges(self):
        # 1 auth-user + 1 blocking + 1 blockedBy + 2 for the ancestor reading (one team).
        with self.assertNumQueries(5):
            self._batch(self.dependent)
        more = [self._task(f"More {i}", "Open", parent=self.parent) for i in range(5)]
        for m in more:
            TaskDependency.objects.create(
                blocker_task=m, blocked_task=self.dependent, team=self.team
            )
        with self.assertNumQueries(5):
            self._batch(self.dependent, self.blocker, self.plain, *more)
