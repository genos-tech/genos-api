"""`GET /api/v2/task/getProjectTasks/`: a constant number of queries (MIP-1416).

The task table, the board, the dashboard and the task diagram all load a project's tasks
through this view, and the diagram does it on every open. Each row's `displayId`
(`MIP-123`) reads `task.project.code`, and the queryset joined only the assignee, so every
row cost one more query: 3,004 queries for a 3,000-task project, measured locally. In
production each of them is a round trip to the database, which is what made the first
open of a big project's diagram take about 7 seconds (Ken, 2026-11-12: MIP-682, in a
project of 1,302 tasks). Later opens were fast only because the response is cached per
project for 5 minutes, until the next task write in that project.

The test counts the queries of a cache MISS for N tasks and for 2N tasks: they must be
equal. It also pins `displayId`, so the join cannot change what the client receives.
"""

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

URL = "/api/v2/task/getProjectTasks/"


class ProjectTasksQueryCountTests(BaseAPITestCase):
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
        self._number = 0

    def add_tasks(self, n):
        rows = []
        for _ in range(n):
            self._number += 1
            rows.append(
                TaskMaster(
                    team=self.team,
                    project=self.project,
                    reporter=self.user,
                    assignee=self.user2 if self._number % 2 else self.user,
                    title=f"Task {self._number}",
                    status="Open",
                    project_task_number=self._number,
                )
            )
        TaskMaster.objects.bulk_create(rows)

    def cold_get(self):
        """One request on a cache miss: the path that is paid after every task write."""
        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(
                URL, {"team_id": str(self.team.team_id), "project_id": self.project.project_id}
            )
        self.assertEqual(response.status_code, 200, response.content)
        return len(ctx), response.json()["data"]["tasks"]

    def test_the_query_count_does_not_grow_with_the_number_of_tasks(self):
        self.add_tasks(10)
        few, rows_few = self.cold_get()
        self.add_tasks(10)
        many, rows_many = self.cold_get()
        self.assertEqual(len(rows_few), 10)
        self.assertEqual(len(rows_many), 20)
        self.assertEqual(few, many, "a query per task is back (see the module docstring)")

    def test_every_row_still_carries_its_display_id(self):
        self.add_tasks(3)
        _, rows = self.cold_get()
        self.assertEqual(sorted(r["displayId"] for r in rows), ["WRD-1", "WRD-2", "WRD-3"])
