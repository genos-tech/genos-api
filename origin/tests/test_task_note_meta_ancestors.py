"""MIP-1667: a task note's row names EVERY task above its own (`ancestors`, root first), not only the
immediate parent, so the notes sidebar can put a sub-task's folder inside its parent's folder at any
depth. Until then the row carried the parent only, and the sidebar could not tell where a grandchild
belonged (it listed the folders side by side).

The meta list (`GET /api/v2/note/task/meta/`), the create response and the move response all carry it
(they share `_task_hierarchy_fields`). The old parent fields stay as they were.
"""

from __future__ import annotations

from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

META = "/api/v2/note/task/meta/"


class Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Roadmap",
            code="RM",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.number = 0
        # The search side is not under test (a refresh would embed through a provider).
        purge = mock.patch("origin.search_engine.purge.purge_entities")
        purge.start()
        self.addCleanup(purge.stop)
        self.authenticate(self.user)

    def task(self, title, parent=None, **kw):
        self.number += 1
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title=title,
            parent_task_id=parent.task_id if parent else None,
            project_task_number=self.number,
            **kw,
        )

    def milestone(self, title):
        """`(backing task, milestone)`: the backing task is the root of the milestone's tasks."""
        backing = self.task(title, is_milestone=True)
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title=title, task=backing, reporter=self.user
        )
        backing.milestone = milestone
        backing.save()
        return backing, milestone

    def note(self, task, title=None):
        return TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            task=task,
            title=title or f"Note on {task.title}",
            owner=self.user,
        )

    def meta(self):
        res = self.client.get(
            META, {"team_id": str(self.team.team_id), "user_id": str(self.user.id)}
        )
        self.assertEqual(res.status_code, 200, res.content)
        return {row["title"]: row for row in res.json()}

    @staticmethod
    def titles(row):
        return [a["title"] for a in row["ancestors"]]


class AncestorsAreRootFirst(Base):
    def test_a_chain_through_a_milestone_is_listed_from_the_milestone_down(self):
        backing, milestone = self.milestone("Launch")
        a = self.task("A", backing, milestone=milestone)
        b = self.task("B", a, milestone=milestone)
        c = self.task("C", b, milestone=milestone)
        for task in (backing, a, b, c):
            self.note(task)
        rows = self.meta()
        self.assertEqual(self.titles(rows["Note on C"]), ["Launch", "A", "B"])
        self.assertEqual(self.titles(rows["Note on B"]), ["Launch", "A"])
        self.assertEqual(self.titles(rows["Note on A"]), ["Launch"])
        # The milestone's own note, and a task with no parent, have nobody above them.
        self.assertEqual(rows["Note on Launch"]["ancestors"], [])

    def test_each_ancestor_says_what_a_folder_needs(self):
        backing, milestone = self.milestone("Launch")
        a = self.task("A", backing, milestone=milestone)
        b = self.task("B", a, milestone=milestone)
        self.note(b)
        chain = self.meta()["Note on B"]["ancestors"]
        self.assertEqual(
            chain,
            [
                {
                    "taskId": backing.task_id,
                    "title": "Launch",
                    "displayId": f"RM-{backing.project_task_number}",
                    "isMilestone": True,
                },
                {
                    "taskId": a.task_id,
                    "title": "A",
                    "displayId": f"RM-{a.project_task_number}",
                    "isMilestone": False,
                },
            ],
        )

    def test_a_task_with_no_parent_has_no_ancestors(self):
        self.note(self.task("Loose"))
        self.assertEqual(self.meta()["Note on Loose"]["ancestors"], [])

    def test_a_loose_chain_without_a_milestone(self):
        root = self.task("Root")
        mid = self.task("Mid", root)
        leaf = self.task("Leaf", mid)
        self.note(leaf)
        self.assertEqual(self.titles(self.meta()["Note on Leaf"]), ["Root", "Mid"])

    def test_a_deep_chain_is_followed_all_the_way_up(self):
        """No product limit on nesting: ten levels are ten ancestors, not the old two."""
        parent = None
        for depth in range(11):
            parent = self.task(f"L{depth}", parent)
        self.note(parent)
        rows = self.meta()
        self.assertEqual(self.titles(rows["Note on L10"]), [f"L{depth}" for depth in range(10)])

    def test_the_parent_fields_are_what_they_were(self):
        backing, milestone = self.milestone("Launch")
        a = self.task("A", backing, milestone=milestone)
        b = self.task("B", a, milestone=milestone)
        self.note(a)
        self.note(b)
        rows = self.meta()
        self.assertEqual(rows["Note on A"]["parentTaskId"], backing.task_id)
        self.assertEqual(rows["Note on A"]["parentTaskTitle"], "Launch")
        self.assertIs(rows["Note on A"]["parentTaskIsMilestone"], True)
        self.assertEqual(rows["Note on B"]["parentTaskId"], a.task_id)
        self.assertEqual(rows["Note on B"]["parentTaskTitle"], "A")
        self.assertIs(rows["Note on B"]["parentTaskIsMilestone"], False)
        self.assertEqual(rows["Note on B"]["parentTaskDisplayId"], f"RM-{a.project_task_number}")


class AWalkThatCannotRunAway(Base):
    def test_a_cycle_ends_the_walk(self):
        x = self.task("X")
        y = self.task("Y", x)
        x.parent_task_id = y.task_id  # X -> Y -> X
        x.save()
        self.note(x)
        self.note(y)
        rows = self.meta()
        # Each sees the other, once, and not itself.
        self.assertEqual(self.titles(rows["Note on X"]), ["Y"])
        self.assertEqual(self.titles(rows["Note on Y"]), ["X"])

    def test_a_task_that_is_its_own_parent_has_no_ancestors(self):
        x = self.task("X")
        x.parent_task_id = x.task_id
        x.save()
        self.note(x)
        self.assertEqual(self.meta()["Note on X"]["ancestors"], [])

    def test_a_parent_that_is_gone_cuts_the_chain_there(self):
        gone = self.task("Gone")
        mid = self.task("Mid", gone)
        leaf = self.task("Leaf", mid)
        self.note(leaf)
        TaskMaster.objects.filter(task_id=gone.task_id).delete()
        row = self.meta()["Note on Leaf"]
        self.assertEqual(self.titles(row), ["Mid"])

    def test_another_teams_task_is_never_read(self):
        """An ancestor that belongs to another team is not in the chain (and its title does not travel)."""
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=other_team, attendee=self.user2)
        other_project = ProjectMaster.objects.create(
            team=other_team, project_name="Theirs", owner=self.user2, project_system_user=self.user2
        )
        theirs = TaskMaster.objects.create(
            team=other_team, project=other_project, title="THEIR SECRET", status="Open"
        )
        mine = self.task("Mine", theirs)
        self.note(mine)
        row = self.meta()["Note on Mine"]
        self.assertEqual(row["ancestors"], [])
        self.assertIsNone(row["parentTaskTitle"])
        self.assertNotIn("THEIR SECRET", str(row))


class OneQueryPerLevelNotPerNote(Base):
    def count_queries(self):
        with CaptureQueriesContext(connection) as ctx:
            self.meta()
        return len(ctx)

    def test_more_notes_at_the_same_depth_cost_no_more_queries(self):
        backing, milestone = self.milestone("Launch")
        a = self.task("A", backing, milestone=milestone)
        b = self.task("B", a, milestone=milestone)
        for i in range(3):
            self.note(self.task(f"leaf {i}", b, milestone=milestone))
        few = self.count_queries()
        for i in range(3, 20):
            self.note(self.task(f"leaf {i}", b, milestone=milestone))
        many = self.count_queries()
        self.assertEqual(few, many, "the ancestors are read per level of the tree, never per note")


class TheCreateAndMoveResponsesCarryIt(Base):
    def test_a_new_note_comes_back_with_its_ancestors(self):
        backing, milestone = self.milestone("Launch")
        a = self.task("A", backing, milestone=milestone)
        b = self.task("B", a, milestone=milestone)
        res = self.client.post(
            "/api/v2/note/task/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "project_id": self.project.project_id,
                "task_id": b.task_id,
                "title": "Fresh",
                "body": [],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual([x["title"] for x in res.json()["ancestors"]], ["Launch", "A"])

    def test_a_task_with_no_parent_comes_back_with_an_empty_list(self):
        loose = self.task("Loose")
        res = self.client.post(
            "/api/v2/note/task/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "project_id": self.project.project_id,
                "task_id": loose.task_id,
                "title": "Fresh",
                "body": [],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["ancestors"], [])
