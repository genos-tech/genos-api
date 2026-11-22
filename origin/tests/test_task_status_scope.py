"""`?statuses=` on the task list endpoints (MIP-1994).

An 800-task milestone (20 active, 780 Closed) made the preview download every Closed task to draw
a 300 px list. The task table, the board and the preview now ask for the statuses they show and
get the per-status counts separately; a request without `statuses` is answered exactly as before
(old clients keep working for one release).
"""

from django.core.cache import cache

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

PROJECT_TASKS = "/api/v2/task/getProjectTasks/"
CHILD_TASKS = "/api/v2/task/childTasks/"


class _Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.client.force_authenticate(user=self.user)
        self._n = 0
        cache.clear()

    def task(self, status="Open", parent=None, **extra):
        self._n += 1
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title=f"Task {self._n}",
            status=status,
            project_task_number=self._n,
            parent_task_id=parent.task_id if parent else None,
            **extra,
        )

    def params(self, **extra):
        return {"team_id": str(self.team.team_id), "project_id": self.project.project_id, **extra}


class ProjectTasksStatusScopeTests(_Base):
    def get(self, **extra):
        cache.clear()
        return self.client.get(PROJECT_TASKS, self.params(**extra))

    def ids(self, response):
        return {int(r["id"]) for r in response.json()["data"]["tasks"]}

    def test_no_param_is_the_old_answer(self):
        a = self.task("Open")
        b = self.task("Closed")
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ids(response), {a.task_id, b.task_id})
        self.assertNotIn("statusCounts", response.json()["data"])

    def test_scope_leaves_out_closed_and_reports_counts(self):
        a = self.task("Open")
        self.task("Closed")
        self.task("Closed")
        c = self.task("WIP")
        response = self.get(statuses="Open,WIP,Blocked,Pending")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ids(response), {a.task_id, c.task_id})
        counts = response.json()["data"]["statusCounts"]
        self.assertEqual(counts["Open"], 1)
        self.assertEqual(counts["WIP"], 1)
        self.assertEqual(counts["Closed"], 2)

    def test_the_cached_answer_is_scoped_too(self):
        a = self.task("Open")
        self.task("Closed")
        self.client.get(PROJECT_TASKS, self.params())  # fills the cache with the full list
        response = self.client.get(PROJECT_TASKS, self.params(statuses="Open"))
        self.assertEqual(self.ids(response), {a.task_id})
        self.assertEqual(response.json()["data"]["statusCounts"]["Closed"], 1)

    def test_a_shown_task_keeps_its_ancestors_even_when_closed(self):
        closed_parent = self.task("Closed")
        child = self.task("Open", parent=closed_parent)
        stray = self.task("Closed")
        response = self.get(statuses="Open")
        self.assertEqual(self.ids(response), {closed_parent.task_id, child.task_id})
        self.assertNotIn(stray.task_id, self.ids(response))

    def test_milestone_backing_rows_are_always_kept(self):
        m = self.task("Closed", is_milestone=True)
        self.task("Closed")
        response = self.get(statuses="Open")
        self.assertEqual(self.ids(response), {m.task_id})

    def test_unknown_status_is_a_400(self):
        self.assertEqual(self.get(statuses="Open,Nope").status_code, 400)

    def test_a_stranger_still_gets_404(self):
        self.client.force_authenticate(user=self.user2)
        response = self.client.get(PROJECT_TASKS, self.params(statuses="Open"))
        self.assertEqual(response.status_code, 404)

    def test_a_delta_still_carries_a_row_that_moved_into_a_hidden_status(self):
        t = self.task("Open")
        full = self.get(statuses="Open")
        since = full.json()["server_time"]
        t.status = "Closed"
        t.save()
        response = self.client.get(PROJECT_TASKS, self.params(statuses="Open", since=since))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ids(response), {t.task_id})
        data = response.json()["data"]
        self.assertEqual(data["totalCount"], 0)  # scoped: nothing with status Open is held
        self.assertEqual(data["idSum"], 0)
        self.assertEqual(data["statusCounts"]["Closed"], 1)


class ChildTasksStatusScopeTests(_Base):
    def setUp(self):
        super().setUp()
        self.parent = self.task("Open")
        self.open = self.task("Open", parent=self.parent)
        self.closed = [self.task("Closed", parent=self.parent) for _ in range(3)]

    def get(self, **extra):
        return self.client.get(
            CHILD_TASKS, {**self.params(**extra), "current_task_id": self.parent.task_id}
        )

    def test_no_param_stays_a_bare_list_of_every_child(self):
        response = self.get()
        self.assertIsInstance(response.json(), list)
        self.assertEqual(len(response.json()), 4)

    def test_scope_returns_an_envelope_with_counts(self):
        response = self.get(statuses="Open,WIP,Blocked,Pending")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([r["id"] for r in body["tasks"]], [self.open.task_id])
        self.assertEqual(body["statusCounts"]["Closed"], 3)
        self.assertEqual(body["statusCounts"]["Open"], 1)

    def test_closed_on_demand(self):
        self.assertEqual(len(self.get(statuses="Closed").json()["tasks"]), 3)

    def test_unknown_status_is_a_400(self):
        self.assertEqual(self.get(statuses="Nope").status_code, 400)

    def test_a_stranger_still_gets_404(self):
        self.client.force_authenticate(user=self.user2)
        self.assertEqual(self.get(statuses="Open").status_code, 404)


class PreviewPayloadSizeTests(_Base):
    """MIP-1994 reported ~300 KB each for `dependency/list` and `milestone/<id>/` on an
    800-task milestone. Neither view reads the child list: pin that, so they cannot start to."""

    def test_dependency_list_of_a_task_with_no_edges_is_tiny(self):
        t = self.task("Open")
        for _ in range(60):
            self.task("Closed", parent=t)
        response = self.client.get("/api/v2/task/dependency/list/", {"task_id": t.task_id})
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(response.content), 200)
