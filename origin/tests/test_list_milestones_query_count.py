"""MIP-1964: `list_milestones` costs a constant number of queries, however many milestones.

It used to run one aggregate per milestone (39 queries for 30 milestones; 111 in one production line).
"""

from __future__ import annotations

from django.conf import settings
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from origin.models.task.milestone_models import MilestoneAssignees
from origin.search_engine.agent.tools import REGISTRY
from origin.tests.test_task_ancestry import AncestryBase

FLAG = "AGENT_CLOSED_ANCESTOR_RULE"


class ListMilestonesQueryCountTests(AncestryBase):
    def _grow(self, count: int) -> None:
        for i in range(count):
            m, backing = self.milestone(f"M{len(self.made)}")
            self._n = 1000 + 10 * len(self.made)  # the backing row took a number
            self.made.append(m)
            self.make("fk child", milestone=m)
            self.make("closed child", milestone=m, status="Closed")
            self.make("parent child", parent=backing)
            MilestoneAssignees.objects.create(milestone=m, user=self.user, team=self.team)

    def _queries(self) -> int:
        with CaptureQueriesContext(connection) as cap:
            result = REGISTRY["list_milestones"].run({}, self.ctx)
        self.assertEqual(len(result["milestones"]), len(self.made))
        for row in result["milestones"]:
            self.assertEqual(row["tasks_total"], 3)
            self.assertEqual(row["tasks_closed"], 1)
            self.assertEqual(row["assignee_count"], 1)
        return len(cap)

    def _check(self) -> None:
        self.made = []
        self._grow(3)
        few = self._queries()
        self._grow(27)
        many = self._queries()
        self.assertEqual(len(self.made), 30)
        self.assertEqual(few, many)

    def test_constant_query_count_flag_off(self):
        self._check()

    @override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, FLAG: True})
    def test_constant_query_count_flag_on(self):
        self._check()
