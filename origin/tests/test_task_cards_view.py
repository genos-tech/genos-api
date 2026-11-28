"""`GET /api/v3/tasks/cards/`: the live fields of the project-chat task cards (MIP-2484)."""

from __future__ import annotations

import datetime as dt

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.services.milestone_rollup import milestone_work_counts

URL = "/api/v3/tasks/cards/"


def _user(name: str) -> CustomUser:
    return CustomUser.objects.create_user(
        username=name, email=f"{name}@example.com", password="pw-123456"
    )


def _para(text: str) -> list:
    return [{"type": "paragraph", "content": [{"type": "text", "text": text, "styles": {}}]}]


class TaskCardsViewTests(TestCase):
    def setUp(self):
        self.member = _user("member")
        self.outsider = _user("outsider")
        self.holder = _user("holder")  # on the team, not on the project
        self.team = TeamMaster.objects.create(
            team_name="Cards", team_email="cards@example.com", owner=self.member
        )
        for u in (self.member, self.holder):
            TeamMembers.objects.create(team=self.team, attendee=u)
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Cards", owner=self.member
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.member)
        self.row = self._task("Launch", status="WIP", is_milestone=True)
        self.milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            task=self.row,
            title="Launch the site",
            status="WIP",
            reporter=self.member,
            description=_para("Ship it."),
        )

    def _task(self, title: str, **kw) -> TaskMaster:
        kw.setdefault("status", "Open")
        kw.setdefault("project", self.project)
        return TaskMaster.objects.create(team=self.team, title=title, **kw)

    def _get(self, user, ids):
        client = APIClient()
        client.force_authenticate(user)
        return client.get(URL, {"ids": ",".join(str(i) for i in ids)})

    def _cards(self, user, ids) -> list[dict]:
        response = self._get(user, ids)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["cards"]

    def test_a_task_card_carries_the_live_row(self):
        task = self._task(
            "Write copy",
            status="WIP",
            priority="High",
            assignee=self.member,
            reporter=self.member,
            due_date=dt.date(2026, 12, 3),
            milestone=self.milestone,
            parent_task_id=self.row.task_id,
            content=_para("Three   plans\n and a table. " + "x" * 400),
            tags=[
                {"tagName": f"t{i}", "tagColor": "#000", "tagTextColor": "#fff"} for i in range(7)
            ],
        )
        [card] = self._cards(self.member, [task.task_id])
        self.assertEqual(card["kind"], "task")
        self.assertEqual(card["displayId"], task.display_id)
        self.assertEqual(
            card["project"], {"projectId": self.project.project_id, "projectName": "Cards"}
        )
        self.assertEqual(
            (card["status"], card["priority"], card["dueDate"]), ("WIP", "High", "2026-12-03")
        )
        self.assertEqual(card["assignee"], {"userId": str(self.member.id), "userName": "member"})
        self.assertEqual(card["milestone"]["title"], "Launch the site")
        self.assertIsNone(card["parent"])  # the milestone's row is not shown twice
        self.assertEqual((len(card["tags"]), card["tagCount"]), (5, 7))
        self.assertTrue(card["excerpt"].startswith("Three plans and a table. xxx"))
        self.assertEqual(len(card["excerpt"]), 160)

    def test_odd_stored_values_do_not_fail(self):
        task = self._task("Odd", content={"unexpected": 1}, tags={"not": "a list"}, status="")
        [card] = self._cards(self.member, [task.task_id])
        self.assertEqual((card["excerpt"], card["tags"], card["status"]), ("", [], "Open"))
        self.assertIsNone(card["assignee"])

    def test_what_may_not_be_read_answers_like_what_does_not_exist(self):
        task = self._task("Secret", assignee=self.member)
        deleted = self._task("Deleted", is_deleted=True)
        gone_project = ProjectMaster.objects.create(
            team=self.team, project_name="Gone", owner=self.member, is_deleted=True
        )
        in_gone = self._task("In a gone project", project=gone_project)
        missing = self._get(self.outsider, [999999]).content
        for ids in ([task.task_id], [self.row.task_id]):
            self.assertEqual(self._get(self.outsider, ids).content, missing)
        self.assertEqual(self._cards(self.member, [deleted.task_id, in_gone.task_id]), [])

    def test_a_removed_member_gets_nothing(self):
        task = self._task("Was mine", assignee=self.holder)
        TeamMembers.objects.filter(attendee=self.holder).delete()
        self.assertEqual(self._cards(self.holder, [task.task_id]), [])

    def test_an_assignee_off_the_project_gets_the_task_only(self):
        parent = self._task("Private parent")
        task = self._task(
            "Given to a holder",
            assignee=self.holder,
            milestone=self.milestone,
            parent_task_id=parent.task_id,
        )
        [card] = self._cards(self.holder, [task.task_id, self.row.task_id, parent.task_id])
        self.assertEqual(card["taskId"], task.task_id)
        self.assertIsNone(card["milestone"])
        self.assertIsNone(card["parent"])

    def test_a_readable_parent_is_named(self):
        parent = self._task("Parent")
        task = self._task("Child", parent_task_id=parent.task_id)
        [card] = self._cards(self.member, [task.task_id])
        self.assertEqual(card["parent"]["title"], "Parent")
        self.assertIsNone(card["milestone"])

    def test_blockers_are_the_unfinished_readable_ones(self):
        task = self._task("Blocked", status="Blocked", assignee=self.holder)
        hidden = self._task("Hidden blocker")
        seen = self._task("Seen blocker", assignee=self.holder)
        done = self._task("Done blocker", status="Closed", assignee=self.holder)
        for blocker in (hidden, seen, done):
            TaskDependency.objects.create(blocker_task=blocker, blocked_task=task, team=self.team)
        [mine] = self._cards(self.member, [task.task_id])
        self.assertEqual(mine["blockedByCount"], 2)
        [theirs] = self._cards(self.holder, [task.task_id])
        self.assertEqual([b["title"] for b in theirs["blockedBy"]], ["Seen blocker"])
        self.assertEqual(theirs["blockedByCount"], 1)

    def test_a_milestone_card_counts_as_the_progress_bar_does(self):
        child = lambda title, **kw: self._task(title, parent_task_id=self.row.task_id, **kw)  # noqa: E731
        child("open")
        child("wip", status="WIP", milestone=self.milestone)
        closed_parent = child("closed", status="Closed")
        self._task(
            "open under closed", parent_task_id=closed_parent.task_id, milestone=self.milestone
        )
        gone = child("gone", status="Deleted")
        self._task("under gone", parent_task_id=gone.task_id, milestone=self.milestone)
        MilestoneAssignees.objects.create(
            team=self.team, milestone=self.milestone, user=self.member
        )
        [card] = self._cards(self.member, [self.row.task_id])
        self.assertEqual(card["kind"], "milestone")
        self.assertEqual((card["title"], card["excerpt"]), ("Launch the site", "Ship it."))
        self.assertEqual(card["assigneeCount"], 1)
        total, closed = milestone_work_counts([self.milestone], ancestor_rule=True)[
            self.milestone.milestone_id
        ]
        self.assertEqual((card["progress"]["total"], card["progress"]["closed"]), (total, closed))
        self.assertEqual(card["progress"]["byStatus"], {"Open": 1, "WIP": 1, "Closed": 2})

    def test_a_deleted_milestone_has_no_card(self):
        self.milestone.is_deleted = True
        self.milestone.save()
        self.assertEqual(self._cards(self.member, [self.row.task_id]), [])

    def test_the_query_count_does_not_grow_with_the_cards(self):
        def make(n):
            made = []
            for i in range(n):
                parent = self._task(f"parent {n}-{i}")
                blocker = self._task(f"blocker {n}-{i}", assignee=self.holder)
                task = self._task(
                    f"task {n}-{i}",
                    status="Blocked",
                    assignee=self.holder,
                    reporter=self.member,
                    parent_task_id=parent.task_id,
                    milestone=self.milestone,
                )
                TaskDependency.objects.create(blocker_task=blocker, blocked_task=task)
                made.append(task.task_id)
            return made

        def count(ids):
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(len(self._cards(self.member, ids)), len(ids))
            return len(queries)

        one = count(make(1) + [self.row.task_id])
        many = count(make(40) + [self.row.task_id])
        self.assertEqual(one, many)
        self.assertLessEqual(many, 13)

    def test_bad_requests(self):
        client = APIClient()
        client.force_authenticate(self.member)
        for query in (
            {},
            {"ids": ""},
            {"ids": "abc"},
            {"ids": "1,"},
            {"ids": "-1"},
            {"ids": "9" * 30},
        ):
            self.assertEqual(client.get(URL, query).status_code, 400, query)
        self.assertEqual(self._get(self.member, range(1, 52)).status_code, 400)
        self.assertEqual(self._get(self.member, range(1, 51)).status_code, 200)

    def test_signing_in_is_required(self):
        self.assertIn(APIClient().get(URL, {"ids": "1"}).status_code, (401, 403))
